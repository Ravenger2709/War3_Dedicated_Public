# Building and validating from source

**This repository is source-only.** It does not ship a prebuilt runtime or an approved release identity. The instructions below document the build inputs; they are **not** a claim that an untested local build is production-ready. The exact build remains deliberately pinned and rejects mismatched source or image IDs.

Requirements: Linux amd64, Docker Engine/BuildKit, Python 3.10+, network access to the official upstream dependencies for the initial builds, and sufficient disk space. Only build from a source revision you trust. None of the commands require proprietary Warcraft, map, account or Star files.

## 1. Build the native host and Wine/compiler bases

```bash
scripts/build-image.sh war3-dedicated:native

docker build --platform linux/amd64 --target runtime \
  -f docker/wine-research/Dockerfile -t war3-wine-research:local docker/wine-research

docker build --platform linux/amd64 --target diagnostics \
  -f docker/wine-research/Dockerfile -t war3-win32-build:local docker/wine-research
```

`scripts/build-image.sh` fetches a pinned GHost++ revision, applies this repository's patch and builds the native Linux components. The Wine base intentionally pins its package snapshot; if a pin becomes unavailable, update it deliberately and repeat validation. The compiler stage builds the small source-based quit helper. No game engine is run.

## 2. Package the production runtime

Find **exact, immutable** local image IDs with `docker image inspect`, then supply them explicitly. Never accept an arbitrary name as proof of an audited base. The production builder originally pinned one specific verified Wine base; this public source provides `--wine-id` to allow a newly rebuilt, explicitly checked base. That changes the build identity and requires entirely new validation.

```bash
NATIVE_ID="$(docker image inspect --format '{{.Id}}' war3-dedicated:native)"
WINE_ID="$(docker image inspect --format '{{.Id}}' war3-wine-research:local)"
HELPER_ID="$(docker image inspect --format '{{.Id}}' war3-win32-build:local)"

python3 scripts/build-production-image.py \
  --native-image war3-dedicated:native --native-id "$NATIVE_ID" \
  --wine-image war3-wine-research:local --wine-id "$WINE_ID" \
  --helper-image war3-win32-build:local --helper-id "$HELPER_ID" \
  --tag war3-production:local --receipt /absolute/private/build-receipt.json
```

The output is a **build receipt**, not an acceptance gate. Do not fabricate a 12-PASS gate, rebrand another release descriptor, or weaken setup's identity checks. Source tests, sanitizer checks, native/relay simulation, startup/shutdown, recovery, persistence, layer/secret audit and a bounded offline soak are prerequisites for an accepted release. Actual iCCup authorization, external client gameplay and AMP lifecycle require their own opt-in integration tests. See the runtime contracts under `docs/contracts/` and the offline probe programs in `tests/phase3/`.

Only after successful checks may a maintainer issue and distribute the version-specific acceptance gate or trusted release identity for the **matching** image and source checkout. Without those inputs, the standalone/AMP startup scripts intentionally refuse activation. Any public image release also needs the third-party notices and licensing review explained in [ATTRIBUTION.md](../ATTRIBUTION.md).

## 3. Operator paths

With that accepted runtime, follow [standalone setup](standalone.md) or [AMP setup](operator-quickstart.md). The operator supplies their own lawful Star/Warcraft/map files **at instance setup, never during the Docker build**, and provides an iCCup account through the private interactive/account UI. `/data` is persistent. Only TCP 6113 should be public; loopback HTTP 8080 and Star relay 16112 must remain unexposed.

To run Python-only focused checks from the checkout, use the standard library runner (some tests additionally require Docker, a compiled native host or synthetic fixtures):

```bash
python3 -m unittest discover -s tests/phase3 -p 'test_*.py'
python3 -m unittest discover -s tests/bridge -p 'test_*.py'
python3 scripts/check-public-tree.py
```

These tests are **not** a substitute for a release gate. The production image and pinned base images must be validated in their actual target environment before hosting users.
