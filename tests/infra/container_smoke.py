#!/usr/bin/env python3
"""Real native image smoke test with private RO map/MPQ mounts and no network.

Requires an existing offline input manifest produced by scripts/prepare-map.py.
No Warcraft executable, game client, account or public service is started.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=40)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[:1000]}")
    return result


def status(name):
    script = "import http.client; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=2); c.request('GET','/status'); r=c.getresponse(); print(r.read().decode())"
    result = docker("exec", name, "python3", "-c", script, check=False)
    return json.loads(result.stdout) if result.returncode == 0 else {}


def until(check, timeout=65):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError("container observation timed out")


def run(image, input_runtime, report):
    started = time.monotonic()
    result = {"tested_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "native container + real GHost; no game client or iCCup; network=none", "checks": [], "result": "FAIL"}
    info = json.loads((input_runtime / "input-manifest.json").read_text())
    mapfile, patch = Path(info["map"]).resolve(), (input_runtime / "game/War3Patch.mpq").resolve()
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (mapfile, patch)]
    names, processes = [], []
    with tempfile.TemporaryDirectory(prefix="war3-container-smoke-") as temp:
        data = Path(temp) / "data"
        subprocess.run(["python3", str(ROOT / "amp/prepare-instance.py"), str(data)], capture_output=True, check=True)
        # Mount actual files explicitly: no symlink to any original install tree
        # is available inside the container.
        (data / "maps/selected.w3x").touch(mode=0o600)
        (data / "warcraft/War3Patch.mpq").touch(mode=0o600)
        (data / "state/persistence-sentinel").write_text("preserve\n")

        def launch():
            name = "war3-infra-" + uuid.uuid4().hex[:12]
            names.append(name)
            log = open(Path(temp) / (name + ".log"), "w")
            args = ["docker", "run", "--name", name, "--label", "war3-dedicated.test=infra",
                    "--network", "none", "--init", "--interactive", "--read-only",
                    "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
                    "--memory", "512m", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m"]
            for directory in ("config", "maps", "warcraft", "logs", "replays", "state"):
                mode = ",readonly" if directory in {"config", "maps", "warcraft"} else ""
                args += ["--mount", f"type=bind,src={data/directory},dst=/data/{directory}{mode}"]
            for source, target in [(mapfile, "/data/maps/selected.w3x"), (patch, "/data/warcraft/War3Patch.mpq")]:
                args += ["--mount", f"type=bind,src={source},dst={target},readonly"]
            proc = subprocess.Popen([*args, image], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True)
            processes.append((proc, log))
            return name, proc, Path(log.name)

        try:
            meta = json.loads(docker("image", "inspect", image).stdout)[0]
            result["image_id"] = meta["Id"]
            assert meta["Config"]["User"] == "10001:10001"
            result["checks"].append("image default USER 10001:10001")
            name, proc, log = launch()
            until(lambda: status(name))
            source_hashes = json.loads(docker("exec", name, "cat", "/opt/war3/share/context-sha256.json").stdout)
            assert all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == checksum for path, checksum in source_hashes.items()), "image source inputs differ from current workspace"
            result["source_sha256"] = source_hashes
            result["ghost_binary_sha256"] = docker("exec", name, "sha256sum", "/opt/war3/bin/ghost++").stdout.split()[0]
            result["checks"].append("image input hashes match current workspace, including native patch and build scripts")
            until(lambda: status(name).get("state") == "LOBBY")
            assert docker("exec", name, "python3", "/opt/war3/healthcheck.py").returncode == 0
            assert docker("exec", name, "python3", "/opt/war3/healthcheck.py", "--ready").returncode == 0
            result["checks"].append("real GHost lobby; liveness and readiness HTTP probes pass inside network=none")
            policy = json.loads(docker("inspect", name).stdout)[0]
            assert policy["HostConfig"]["ReadonlyRootfs"]
            assert policy["HostConfig"]["NetworkMode"] == "none"
            assert "ALL" in policy["HostConfig"]["CapDrop"]
            script = "import os; from pathlib import Path; assert os.getuid()!=0; assert all(not Path(p).exists() for p in ('/usr/bin/wine','/usr/bin/Xvfb','/usr/bin/Xorg')); print('ok')"
            docker("exec", name, "python3", "-c", script)
            for target in ("/opt/war3/forbidden", "/data/config/forbidden", "/data/maps/forbidden", "/data/warcraft/forbidden"):
                probe = docker("exec", name, "python3", "-c", f"open({target!r},'w').write('forbidden')", check=False)
                assert probe.returncode != 0
            result["checks"].append("non-root; read-only root/config/maps/Warcraft; no Wine or X executables")
            docker("exec", name, "python3", "-c", "from pathlib import Path; Path('/data/replays/persistence-sentinel').write_text('preserve\\n')")
            pid = status(name)["pid"]
            docker("exec", name, "python3", "-c", f"import os; os.kill({int(pid)},9)")
            until(lambda: status(name).get("restarts", 0) >= 1)
            until(lambda: status(name).get("state") == "LOBBY")
            assert status(name)["pid"] != pid
            result["checks"].append("SIGKILL native child -> bounded recovery -> new lobby")
            proc.stdin.write("status\nstop\n"); proc.stdin.flush()
            until(lambda: status(name).get("state") == "STOPPED")
            assert docker("exec", name, "python3", "/opt/war3/healthcheck.py").returncode == 0
            assert docker("exec", name, "python3", "/opt/war3/healthcheck.py", "--ready", check=False).returncode == 1
            proc.stdin.write("start\n"); proc.stdin.flush()
            until(lambda: status(name).get("state") == "LOBBY")
            result["checks"].append("local Docker stdin status/stop/start; stopped child has live=200 ready=503")
            graceful_before = (data / "logs/ghost.log").read_text().count("all threads finished, exiting nicely")
            proc.send_signal(signal.SIGTERM)  # Same path used by Generic AMP.
            assert proc.wait(timeout=30) == 0, "Docker CLI did not exit cleanly"
            assert "all threads finished, exiting nicely" in log.read_text(), "final native diagnostics were not preserved on supervisor stdout"
            assert (data / "logs/ghost.log").read_text().count("all threads finished, exiting nicely") == graceful_before + 1, "native GHost graceful completion not observed"
            stopped = json.loads((data / "logs/supervisor/status.json").read_text())
            assert stopped["last_exit_code"] == 0 and stopped["state"] == "STOPPED", "supervisor did not reap native child with exit 0"
            assert json.loads(docker("inspect", name).stdout)[0]["State"]["ExitCode"] == 0
            result["checks"].append("SIGTERM to Docker CLI proxies through init; native child and supervisor exit 0")
            name, proc, _ = launch()
            until(lambda: status(name).get("state") == "LOBBY")
            assert (data / "state/persistence-sentinel").read_text() == "preserve\n"
            assert (data / "state/ghost.dbs").is_file()
            assert (data / "logs/ghost.log").is_file()
            assert docker("exec", name, "cat", "/data/replays/persistence-sentinel").stdout == "preserve\n"
            result["checks"].append("new container with same mounts: SQLite, log, state/replay sentinels persist; new lobby")
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == 0
            cfg = data / "config/ghost.cfg"
            cfg.write_text(cfg.read_text().replace("autohost_maxgames = 1", "autohost_maxgames = 0"))
            name, proc, idle_log = launch()
            until(lambda: status(name).get("state") == "READY")
            assert docker("exec", name, "python3", "/opt/war3/healthcheck.py", "--ready").returncode == 0
            assert "offline initialization complete" in idle_log.read_text()
            assert "creating game [" not in idle_log.read_text()
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=30) == 0
            result["checks"].append("autohost disabled: genuine validated native initialization marker -> READY without lobby")
            assert hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in (mapfile, patch)]
            assert all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == checksum for path, checksum in source_hashes.items()), "workspace inputs changed while container tests were running"
            result["checks"].append("original map and patch SHA256 unchanged")
            result["result"] = "PASS"
        except BaseException as error:
            result["error"] = f"{type(error).__name__}: {error}"
            # Logs contain only our offline config, no account data.
            result["diagnostic"] = [Path(log.name).read_text()[-3500:] for _, log in processes]
            raise
        finally:
            for name in names:
                docker("rm", "--force", name, check=False)
            for proc, log in processes:
                if proc.poll() is None:
                    proc.wait(timeout=5)
                if proc.stdin:
                    proc.stdin.close()
                log.close()
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            result["containers_removed"] = names
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="war3-dedicated:infra-test")
    parser.add_argument("--input-runtime", required=True, type=Path)
    parser.add_argument("--report", default=ROOT / "tests/infra/container-result.json", type=Path)
    args = parser.parse_args()
    run(args.image, args.input_runtime.resolve(), args.report)
