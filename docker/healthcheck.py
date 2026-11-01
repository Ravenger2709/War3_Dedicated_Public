#!/usr/bin/env python3
"""Bounded local liveness/readiness probe; never reads account configuration."""
import argparse
import http.client
import json


def check(port=8080, ready=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/health/ready" if ready else "/health/live")
        response = connection.getresponse()
        body = json.loads(response.read(65536))
        return response.status == 200 and isinstance(body, dict) and body.get("ready" if ready else "alive") is True
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    raise SystemExit(0 if check(args.port, args.ready) else 1)
