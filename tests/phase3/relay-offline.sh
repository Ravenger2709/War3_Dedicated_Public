#!/usr/bin/env bash
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
exec docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 10001:10001 \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 --entrypoint python3 \
  --mount "type=bind,src=$task_root/src/bridge/star_relay.py,dst=/opt/star-hosting/src/bridge/star_relay.py,readonly" \
  --mount "type=bind,src=$task_root/tests/bridge/test_star_relay.py,dst=/tmp/test_star_relay.py,readonly" \
  sha256:fd37203f6094d366567f882ac1dfaa02087b003cfed3e9f8fbebd247cc397b16 \
  -m unittest discover -s /tmp -p test_star_relay.py -q
