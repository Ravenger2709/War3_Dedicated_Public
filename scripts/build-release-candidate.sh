#!/usr/bin/env bash
# Build a source-bound local candidate; acceptance is a separate process.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
if [[ $# != 2 ]]; then
    echo 'Usage: scripts/build-release-candidate.sh IMAGE:TAG /absolute/path/build-receipt.json' >&2
    exit 2
fi
case "$2" in /*) ;; *) echo 'Receipt path must be absolute' >&2; exit 2 ;; esac
[[ ! -e "$2" ]] || { echo 'Receipt already exists' >&2; exit 2; }
command -v docker >/dev/null
scripts/build-image.sh war3-release-input:native
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 --target runtime \
    -f docker/wine-research/Dockerfile -t war3-release-input:wine docker/wine-research
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 --target diagnostics \
    -f docker/wine-research/Dockerfile -t war3-release-input:helper docker/wine-research
native_id=$(docker image inspect --format '{{.Id}}' war3-release-input:native)
wine_id=$(docker image inspect --format '{{.Id}}' war3-release-input:wine)
helper_id=$(docker image inspect --format '{{.Id}}' war3-release-input:helper)
python3 scripts/build-production-image.py \
    --native-image war3-release-input:native --native-id "$native_id" \
    --wine-image war3-release-input:wine --wine-id "$wine_id" \
    --helper-image war3-release-input:helper --helper-id "$helper_id" \
    --tag "$1" --receipt "$2"
