#!/usr/bin/env bash
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
image_tag=${WAR3_IMAGE:-war3-dedicated:local}
if [[ $# -gt 1 ]]; then
    echo 'Usage: scripts/build-image.sh [image:tag]' >&2
    exit 2
fi
if [[ $# -eq 1 ]]; then image_tag=$1; fi
context_dir=$(mktemp -d "${TMPDIR:-/tmp}/war3-image-context.XXXXXXXX")
trap 'rm -rf -- "$context_dir"' EXIT
python3 "$project_root/docker/prepare_context.py" "$context_dir"
docker build --platform linux/amd64 --target runtime \
    --label org.opencontainers.image.title=war3-dedicated \
    --tag "$image_tag" --file "$context_dir/docker/Dockerfile" "$context_dir"
