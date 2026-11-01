# Operator quickstart

Use a trusted release checkout and its maintainer-exported release identity. The runtime image and identity can be downloaded from GitHub Releases as described in [standalone setup](standalone.md), or built and validated following [source build and validation](build-from-source.md). Use an
already installed Docker service and AMP, and your own licensed Star/Warcraft
files. Routine setup needs no AI, manual 31-file copying, or host dependency
installation. The setup command prepares files locally; AMP Start later starts
the runtime and original Star authentication.

## 1. Make the two images available

The release maintainer builds and accepts the runtime through the production
release process. Obtain its immutable `repository@sha256:…` pull reference and
`release-identity.json` through your trusted release channel. Treat that JSON as
an approval document: receiving an arbitrary JSON from an unknown source does
not establish trust. It contains public gate/source hashes and image metadata,
with no proprietary assets or account credentials.

If using a registry rather than the release archive, authenticate as needed and pull the supplied runtime and control digest references:

```bash
docker pull "$RUNTIME_PULL_REF"
docker pull "$CONTROL_PULL_REF"
```

Alternatively build the trusted control image from its pinned base images:

```bash
docker build -t war3-amp-control:phase3-candidate docker/amp-control
```

Select the trusted control image in the AMP template's specific Docker image
setting (the supplied template uses `war3-amp-control:phase3-candidate`; a pulled
control image can be tagged to that local name). Container policy must remain
**Required**. The control image contains AMP/Python/Docker CLI; the runtime
contains Wine/native services. Give only the trusted control container the
extra `/var/run/docker.sock` mount. The actual runtime receives no Docker socket.
Setup never changes repository or registry visibility. It does not log in,
pulls, builds, tags, pushes or changes an image.

## 2. Create a stopped managed AMP instance

Use ADS's ordinary instance wizard with the War3 production Generic template.
API integrations must use `ADSModule/CreateInstanceFromSpec` with managed
configuration enabled: verify **ManagementMode=10**. Do not force
`AutoConfigure=false`, which creates a standalone mode 0 instance. Keep this
new instance stopped until setup and account/map configuration are complete.

The application directory must be a **new** `war3-production` directory under
that instance's host directory. If AMP already created an empty application
directory, verify its exact path and remove only that empty directory with
`rmdir`. Setup refuses every existing destination, including empty directories
and links. It never replaces another server's files.

## 3. Run one setup command as the data owner

Run from the trusted release checkout on the Docker host as the non-root owner
of the AMP instance data. Its UID/GID must also be the runtime data owner. The
destination's parent must already exist, be owned by this user, and not be
group/world writable. Use the **host** instance path, not the control container's
`/AMP` path: it is recorded for the host Docker daemon's bind mounts.

```bash
python3 scripts/setup-production-instance.py --noninteractive \
  --release-identity /absolute/trusted/release-identity.json \
  --image "$RUNTIME_PULL_REF" \
  --instance-dir "$AMP_HOST_INSTANCE/war3-production" \
  --star-dir /absolute/licensed/Star \
  --game-dir /absolute/licensed/Warcraft-III-1.26a \
  --redist /absolute/licensed/vc_redist.x86.exe \
  --map /absolute/licensed/chosen.w3x
```

Omit `--noninteractive` to be prompted for missing paths. There are no profile,
username or password arguments. `--redist` is optional when the VC2015 x86
installer is at `Star/vc_redist.x86.exe`, `Star/redist/vc_redist.x86.exe`, or
`Star/Redist/vc_redist.x86.exe`; setup checks only these named locations.
`--image-id` is an alias for `--image`; either may select an already-local image
ID or reference. If omitted, setup uses the identity's immutable pull reference,
or its original approved ID when no pull reference was exported.

Before copying assets, setup checks the original main-approved 12-PASS gate,
its source digest, the local image's **entire Config**, ordered RootFS layers,
and linux/amd64 platform against the trusted identity. This permits the known
Docker index-ID/classic-config-ID difference only when all those values match
exactly. `acceptance.json` then uses the local inspect ID, while
`release-correlation.json` preserves the original approved gate, all source
hashes and checks, trusted-input hash, and image correlation. The existing
production gate validator remains strict. Without a release identity,
`--gate /absolute/main-approved.json` is available only for an **exact** accepted
local image ID; it cannot translate IDs.

Use the checkout paired with that accepted release. Setup verifies the static
configuration-validator source hashes against the gate, then parses the final
merged settings and checks their runtime mount paths before copying assets.
Example gameplay defaults such as the display name can still be merged normally.
Newer settings schemas cannot silently be paired with an older accepted runtime.
Native LAN advertisements stay on loopback.

Setup validates the release-approved `config/star-vendor-files.json` and copies
only its 31 named Star files. It verifies the VC2015 installer SHA-256:

```text
fdd1e1f0dcae2d0aa0720895eff33b927d13076e64464bb7c7e5843b7667cd14
```

It reads only the explicitly selected map, the named Star/redist inputs, and
`war3.exe`, `game.dll`, `War3Patch.mpq` under the provided game directory. The two
PE files go to `vendor/game` as metadata, the MPQ to `data/warcraft`, and the map
to `data/maps/selected.w3x` (`.w3m` input is also accepted). It never executes PE,
Wine or the game engine. Original bytes and permissions are left unchanged;
profiles, cached credentials and the rest of the game installation are neither
read nor copied. Source paths and ancestors must be real paths without links;
source files must be regular, single-link files.

Before publishing the new tree, setup runs only the accepted image's Python
map analyzer with the copied map mounted read-only. It uses no network,
Wine, engine, profile or account input. Limits are 30 seconds, 64 KiB output,
256 MiB memory, 32 processes and one CPU. Its whole-map SHA must match the
validated input. A timeout, failed analyzer or invalid result removes staging.

The prepared tree has directories mode 0700 and files mode 0600, owned by the
data owner. Partial failures remove only the private staging tree. Publishing
uses Linux atomic no-replace rename, so a destination created during setup also
survives untouched. Do not run setup as root or point it inside an original
Star/game installation.

## 4. Configure account and map, then Start

In the integrated release, enter the bot account in the new instance's **Account**
UI, or edit `control/account-input.json` through AMP File Manager. The account
helper supplied by that release generates the private autologin profile under
`vendor/profile` at Start. Setup does not copy an existing profile, accept a
password argument, authenticate, or generate autologin credentials itself.

The File Manager root is already `war3-production`: open
`data/config/presets.json`, `vendor/profile`, and `control/account-input.json`
directly, without another `war3-production/` prefix. Keep these private files
out of Git, issue attachments, images and release artifacts.

Setup writes one enabled preset to `data/config/presets.json`, using the chosen
map's `selected.w3x` filename, and selects its ID in `amp-settings.json`.
Ordinary maps get `selected-map`, their analyzed title/player capacity, AI off,
automatic countdown off, and thresholds bounded by human slots. No initial map
filename editing is needed, including when the release has no presets example.
Maps without two human slots need an explicit supported AI preset; setup rejects
an unsupported one rather than preparing an unusable lobby.

The exact Footman map can instead get `footman-ultimate-ai`: the source preset
example must match its gate hash, and the whole-map SHA, script SHA and detected
features must match the curated proof. Only its filename changes; the original
review and `runtime_tested` value remain intact. Candidate detection never grants
approval. Setup refreshes the staged selector manifest; after installing or
refreshing the AMP template, verify **Map preset** shows that generated ID.
For later maps, edit the JSON, add the matching files and apply/refresh the
selector through the supplied AMP actions. The gameplay display name defaults
to `War3HostBot`; runtime validation remains authoritative.

Use AMP **Start**, **Stop**, **Restart** for routine operations. Account and map
changes apply through the integrated helpers on Start/Restart. The runtime
publishes TCP 6113 only; ensure your existing network permits your intended
clients. Setup does not configure routers, contact a remote host or test login.
An accepted release and successful setup do not prove a new operator's account,
WAN connectivity or multiplayer gameplay.

## Maintainer: export a release identity

Run this only with the actual main-approved gate and its exact accepted local
image ID. No approval is invented from a build receipt, a tag or equivalent
image contents:

```bash
python3 scripts/export-release-identity.py \
  --gate /absolute/private/main-approved.json \
  --image-id "$EXACT_ACCEPTED_IMAGE_ID" \
  --pull-ref "$RUNTIME_PULL_REF" \
  --output /absolute/release/release-identity.json
```

The output path must be new. `--pull-ref` is optional; when supplied it must be
an immutable reference already present in the accepted image's local
`RepoDigests`. Export inspects only that local image, calls `production.gate`
with the original ID, verifies its source-digest label, and exports the complete
Config, RootFS and platform plus the unmodified gate. Distribute the resulting
JSON through the existing trusted release process. This helper does not change
GitHub publication, package visibility or CI workflows.
