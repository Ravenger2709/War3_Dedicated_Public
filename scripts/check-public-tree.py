#!/usr/bin/env python3
"""Conservative publication guard: paths and pattern types only, never secret values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_EXTENSIONS = {"", ".md", ".txt", ".py", ".sh", ".c", ".cpp", ".h", ".json", ".ini", ".cfg", ".kvp", ".patch", ".yml", ".yaml", ".gitignore", ".dockerignore"}
BINARY_EXTENSIONS = {".w3x", ".w3m", ".mpq", ".exe", ".dll", ".sqlite", ".sqlite3", ".db", ".pfx", ".p12", ".pem", ".key", ".zip", ".tar", ".gz", ".icc", ".w3g"}

# Exclude this definition file so the patterns themselves are not self-matches.
PATTERNS = [
    ("internal IPv4 address", re.compile(r"\b(?:192\.168\.(?:\d{1,3}\.)\d{1,3}|10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")),
    ("real home directory", re.compile(r"/home/[a-zA-Z0-9_.-]+/")),
    ("internal hostname", re.compile(r"(?i)\b(?:ci-cd|second-host-vm|amp-control-vm|war3phase3container01)\b")),
    ("private service hostname", re.compile(r"(?i)(?:loginto\.me|greenzone-servers)")),
    ("private image registry", re.compile(r"(?i)ghcr\.io/ravenger2709/war3_dedicated")),
    ("old private repo URL", re.compile(r"(?i)github\.com/ravenger2709/war3_dedicated(?!_public)\b")),
    ("credential-like token", re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{24,}|github_pat_[A-Za-z0-9_]{32,}|gsk_[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
]


def paths():
    process = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False)
    if process.returncode == 0 and process.stdout:
        return [Path(p.decode()) for p in process.stdout.split(b"\0") if p]
    return [p.relative_to(ROOT) for p in ROOT.rglob("*") if p.is_file() and not any(part in {".git", "__pycache__", ".venv"} for part in p.relative_to(ROOT).parts)]


def main():
    failures=[]; files=0
    for rel in sorted(paths()):
        full=ROOT/rel
        files+=1
        if full.is_symlink() or not full.is_file() or full.stat().st_size>2*1024*1024:
            failures.append((rel,"unsafe file kind/size"));continue
        if rel.suffix.lower() in BINARY_EXTENSIONS or rel.suffix.lower() not in ALLOWED_EXTENSIONS:
            failures.append((rel,"unexpected filename/extension"));continue
        if any(part.lower() in {"vendor","artifacts","private","releases"} for part in rel.parts) or                 rel.as_posix().startswith("research/evidence/"):
            failures.append((rel,"private/application tree"));continue
        try:
            content=full.read_bytes()
            if b"\0" in content:raise UnicodeError("binary")
            decoded=content.decode("utf-8")
        except UnicodeError:
            failures.append((rel,"non-UTF8/binary input"));continue
        if rel.as_posix()=="scripts/check-public-tree.py":continue
        for label, expression in PATTERNS:
            if expression.search(decoded):failures.append((rel,label))
    if failures:
        for rel,label in sorted(set(failures)):
            print(f"BLOCKED {rel.as_posix()}: {label}",file=sys.stderr)
        print(f"BLOCKED {len(failures)} privacy/source issue(s)",file=sys.stderr)
        return 1
    print(f"PASS: {files} tracked text files; no blocked paths, private hosts/IPs or recognized credential signatures")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
