#!/usr/bin/env python3
"""Verify a main-accepted local image against checkout and audit every saved layer."""
import argparse
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "amp"))
from production import DOCKER, docker_json, environment, gate, read_json

spec = importlib.util.spec_from_file_location("production_context", ROOT / "docker/production/prepare_context.py")
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


def prohibited(name):
    path = PurePosixPath(name.lower())
    base = path.name
    return (base in {"launcher.exe", "launcher.ini", "war3.exe", "game.dll", "vc_redist.x86.exe"}
            or base.startswith(("iccguard", "icc_guard")) or path.suffix in {".mpq", ".w3x", ".w3m", ".icc"}
            or str(path).lstrip("./").startswith(("vendor/star/", "vendor/profile/", "vendor/game/", "vendor/redist/",
                                                    "data/prefix/", "data/session/", "auth-profile/")))


def archive_name(value):
    if not isinstance(value, str) or not value:
        raise ValueError("invalid image archive reference")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid image archive reference")
    return path.as_posix()


def referenced_layers(documents):
    """Resolve saved-image manifests so omitted or unreadable layers fail closed."""
    expected = set()
    if "manifest.json" in documents:
        manifests = documents["manifest.json"]
        if not isinstance(manifests, list) or not manifests:
            raise ValueError("invalid Docker save manifest")
        for manifest in manifests:
            if not isinstance(manifest, dict) or not isinstance(manifest.get("Layers"), list):
                raise ValueError("invalid Docker save manifest")
            expected.update(archive_name(name) for name in manifest["Layers"])

    visited = set()

    def blob_name(descriptor):
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("digest"), str):
            raise ValueError("invalid OCI descriptor")
        digest = descriptor["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("unsupported OCI digest")
        return "blobs/sha256/" + digest[7:]

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        document = documents.get(name)
        if not isinstance(document, dict) or document.get("schemaVersion") != 2:
            raise ValueError("missing or invalid OCI image manifest")
        if isinstance(document.get("manifests"), list) and document["manifests"]:
            for descriptor in document["manifests"]:
                visit(blob_name(descriptor))
        elif isinstance(document.get("layers"), list):
            for descriptor in document["layers"]:
                name = blob_name(descriptor)
                if descriptor.get("mediaType") == "application/vnd.in-toto+json":
                    # BuildKit also saves its provenance as an OCI artifact
                    # manifest. This JSON is not a rootfs tar layer. Require
                    # the referenced statement to exist and be recognizable;
                    # ordinary/unknown JSON layers still fail the rootfs audit.
                    statement = documents.get(name)
                    if (not isinstance(statement, dict)
                            or statement.get("_type") not in {"https://in-toto.io/Statement/v0.1", "https://in-toto.io/Statement/v1"}
                            or statement.get("predicateType") != "https://slsa.dev/provenance/v0.2"
                            or not isinstance(statement.get("predicate"), dict)
                            or not isinstance(statement.get("subject"), list) or not statement["subject"]):
                        raise ValueError("missing or invalid BuildKit provenance statement")
                    for subject in statement["subject"]:
                        if (not isinstance(subject, dict) or not isinstance(subject.get("name"), str)
                                or not isinstance(subject.get("digest"), dict)
                                or not re.fullmatch(r"[0-9a-f]{64}", subject["digest"].get("sha256", ""))):
                            raise ValueError("invalid BuildKit provenance subject")
                else:
                    expected.add(name)
        else:
            raise ValueError("unsupported OCI manifest")

    if "index.json" in documents:
        visit("index.json")
    if not expected:
        raise ValueError("saved image has no referenced filesystem layers")
    return expected


def audit_layers(image_id):
    """Scan all historical layers too; deleting a private file in a later layer is insufficient."""
    process = subprocess.Popen([*DOCKER, "image", "save", image_id], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=environment())
    scanned, documents, names = set(), {}, set()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as saved:
            for member in saved:
                name = archive_name(member.name)
                # Docker save uses layer.tar; OCI variant uses blobs/sha256/* for both JSON and tar.
                traditional = name.endswith("/layer.tar")
                oci_blob = name.startswith("blobs/sha256/")
                manifest = name in {"manifest.json", "index.json"}
                if not (traditional or oci_blob or manifest) or member.isdir():
                    continue
                if not member.isfile() or name in names:
                    raise ValueError("duplicate or non-regular image archive input")
                names.add(name)
                # Keep at most one blob on disk. Rewinding is necessary to distinguish
                # genuine JSON metadata from an unsupported/corrupt compressed layer.
                with tempfile.TemporaryFile() as blob:
                    with saved.extractfile(member) as source:
                        shutil.copyfileobj(source, blob)
                    blob.seek(0)
                    try:
                        if manifest:
                            raise tarfile.ReadError()
                        layer = tarfile.open(fileobj=blob, mode="r|*")
                    except tarfile.ReadError:
                        if traditional or member.size > 2 * 1024 * 1024:
                            raise ValueError("unsupported or corrupt filesystem layer") from None
                        blob.seek(0)
                        try:
                            document = json.load(blob)
                        except (ValueError, UnicodeError):
                            raise ValueError("unsupported or corrupt OCI blob") from None
                        if not isinstance(document, (dict, list)):
                            raise ValueError("invalid image JSON metadata")
                        documents[name] = document
                        continue
                    with layer:
                        for entry in layer:
                            if not entry.isdir() and prohibited(entry.name):
                                raise ValueError("proprietary/secret runtime input found in image layer")
                    scanned.add(name)
        if process.wait(timeout=30) != 0 or not referenced_layers(documents) <= scanned:
            raise ValueError("image layer archive unsupported or incomplete")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
    return len(scanned)


def verify(image_id, acceptance):
    accepted = read_json(acceptance)
    digest = gate(accepted, image_id)
    with tempfile.TemporaryDirectory(prefix="war3-release-context-") as temp:
        hashes = context.prepare(ROOT, temp)
    if hashes != accepted["source_sha256"]:
        raise ValueError("accepted image is stale relative to checkout")
    meta = docker_json(["image", "inspect", image_id])[0]
    labels = meta.get("Config", {}).get("Labels") or {}
    if (meta["Id"] != image_id or meta["Architecture"] != "amd64" or meta["Os"] != "linux"
            or labels.get("war3.production.source-digest") != digest
            or labels.get("org.opencontainers.image.source") != "https://github.com/Ravenger2709/War3_Dedicated_Public"):
        raise ValueError("image identity, platform or source label mismatch")
    layers = audit_layers(image_id)
    return {"schema_version": 1, "image_id": image_id, "source_digest": digest,
            "checks": accepted["checks"], "layer_audit": "PASS", "layers_scanned": layers,
            "gameplay_green": False, "amp_green": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--gate", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.image_id, args.gate)
        with args.receipt.open("x", encoding="utf-8") as output:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")
        print(json.dumps(result))
    except (OSError, ValueError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        parser.exit(2, "Release rejected: accepted image/source/layer audit incomplete.\n")
