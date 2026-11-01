# Optional AMP control image

This source image extends the official pinned CubeCoders `ampbase:debian` image with a pinned Docker CLI and a small supplemental-group-aware `gosu` wrapper. It runs **AMP itself** and controls a separate accepted War3 runtime container on the same host daemon. The runtime does not receive the Docker socket. The official AMP startup entrypoint is preserved.

**Security:** mounting `/var/run/docker.sock` into the trusted AMP control container grants Docker-daemon authority, effectively host-root privileges. Use only with an AMP installation and operator account you trust. Do not expose this control image to untrusted users or claim Docker's read-only mount restricts API powers.

Build from the repository root:

```bash
docker build --platform linux/amd64 -t war3-amp-control:local docker/amp-control
```

The template `amp/war3-production.kvp` defaults to `war3-amp-control:phase3-candidate`. If you use your own verified image, configure the matching tag in the local template before import. Runtime image, trusted release identity and account/map setup are separate and required. See [AMP operator guide](../../docs/operator-quickstart.md). This build runs no licensed Warcraft/Star assets and does not deploy or start an AMP instance.

Sources and credits: [CubeCoders dockerfiles](https://github.com/CubeCoders/dockerfiles), [Docker CLI](https://github.com/docker-library/docker), [AMPTemplates](https://github.com/CubeCoders/AMPTemplates).
