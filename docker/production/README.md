# Production Docker image packaging

`Dockerfile` assembles Python supervisor/runtime, patched native GHost++, Wine/Xvfb and an open-source-compiled quit helper from **three explicit base image IDs**. `prepare_context.py` creates an allowlisted text-only build context; it intentionally refuses missing `src/runtime` and missing helper sources. No Warcraft III binaries, map, Star binaries, profile or account input is accepted in the build context.

Follow [source build instructions](../../docs/build-from-source.md). A successful build receipt is **not** a release acceptance gate. Private credentials and licensed Star/Warcraft files are mounted **only at instance setup**, not embedded in any Docker layer. Only TCP 6113 is public; HTTP 8080 and the relay 16112 bind to loopback inside the runtime. Use a non-root runtime UID and preserve data/config/maps/warcraft ownership and bind contracts.
