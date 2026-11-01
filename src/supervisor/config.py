"""Strict, non-secret supervisor configuration."""
import json
import math
from pathlib import Path


def load_config(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration must be an object")
    if set(raw) - {"mode", "hostbot", "http", "runtime_dir", "recovery", "iccup", "gameplay"}:
        raise ValueError("unknown supervisor configuration key")
    mode = raw.get("mode", "offline")
    if mode not in ("offline", "iccup"):
        raise ValueError("invalid mode")
    hostbot = raw.get("hostbot", {})
    if set(hostbot) - {"command", "cwd", "startup_timeout", "stop_timeout", "lobby_timeout"}:
        raise ValueError("unknown hostbot configuration key")
    command = hostbot.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(x, str) and x and "\x00" not in x for x in command
    ):
        raise ValueError("hostbot.command must be a nonempty argv array")
    http = {"host": "127.0.0.1", "port": 8080, **raw.get("http", {})}
    if set(http) - {"host", "port"} or not isinstance(http["host"], str):
        raise ValueError("invalid http configuration")
    if type(http["port"]) is not int or not 0 <= http["port"] <= 65535:
        raise ValueError("invalid HTTP port")
    recovery = {"max_restarts": 5, "backoff_initial": 1.0, "backoff_max": 60.0,
                "stable_seconds": 300.0, **raw.get("recovery", {})}
    if set(recovery) - {"max_restarts", "backoff_initial", "backoff_max", "stable_seconds"}:
        raise ValueError("unknown recovery configuration key")
    if type(recovery["max_restarts"]) is not int or not 0 <= recovery["max_restarts"] <= 100:
        raise ValueError("max_restarts must be between 0 and 100")
    hostbot = {"cwd": ".", "startup_timeout": 90.0, "stop_timeout": 10.0,
               "lobby_timeout": 3600.0, **hostbot}
    for obj, keys in [(hostbot, ("startup_timeout", "stop_timeout", "lobby_timeout")),
                      (recovery, ("backoff_initial", "backoff_max", "stable_seconds"))]:
        for key in keys:
            val = obj[key]
            if type(val) not in (int, float) or not math.isfinite(val) or val <= 0:
                raise ValueError(f"{key} must be a positive finite number")
    if recovery["backoff_initial"] > recovery["backoff_max"]:
        raise ValueError("backoff_initial exceeds backoff_max")
    if not isinstance(hostbot["cwd"], str) or not isinstance(raw.get("runtime_dir", "runtime"), str):
        raise ValueError("paths must be strings")
    result={"mode": mode, "hostbot": hostbot, "http": http,
            "runtime_dir": raw.get("runtime_dir", "runtime"), "recovery": recovery}
    if "gameplay" in raw or "iccup" in raw:
        from src.runtime.config import gameplay_config
        result["gameplay"]=gameplay_config(raw.get("gameplay",{}))
    if "iccup" in raw:
        from src.runtime.config import iccup_config
        result["iccup"]=iccup_config(raw["iccup"])
        if mode=="iccup" and http["host"] not in ("127.0.0.1","::1"):
            raise ValueError("production HTTP must bind loopback")
    return result
