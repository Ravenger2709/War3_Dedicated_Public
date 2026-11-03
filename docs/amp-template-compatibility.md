# AMP template compatibility comparison

Keep the current ampbase-derived control container and supervised sibling
runtime. The official templates provide useful examples of presentation,
foreground process management, configuration mapping and readiness; they do not
establish a need to replace this working architecture.

## Scope and evidence

Official source: the clean local clone `/tmp/war3-amp-templates-readonly`, commit
`36d02437561729635ca5f07574d7fb58e7c41bfc`. All three KVPs and their complete
config, update, metaconfig and port companions were read; the Contagion config
template was also inspected. Their pinned primary URLs are below. The web cache
could not fetch Contagion, so its comparison uses the verified Git checkout.

Our template/control comparison starts at main commit
`442fd537ff8cf797a4768d60719a5e5333eb13b8`; subsequent maintainer observations
on 2026-10-30 supplement that source baseline. This is a documentation review:
no existing AMP configuration, account, image or runtime was changed.

The maintainer confirmed through `Core.GetModuleInfo` that the installed AMP is
**Proteus 2.8.0.10**, build **20261028.1**, API **2.8.0.10**, with instance
BasePort **8084** and PrimaryEndpoint **0.0.0.0:6113**. This is not an AMP 2.7
execution test. No older binary, licence or credential discovery is needed for
this comparison.

## What the official templates actually do

| Area | Longvinter Wine | Contagion | ASKA | War3 production |
| --- | --- | --- | --- | --- |
| Linux launch | `/usr/bin/wine`, Windows server executable | `/usr/bin/xvfb-run -a wine`, Windows server executable | `/usr/bin/xvfb-run -a wine`, Windows server executable | AMP runs Python, which validates inputs and execs foreground `docker run`; the runtime supervises native GHost, original Star and relay |
| Container policy | Recommended on Linux; `ampbase:wine-stable` | Recommended on Linux; `ampbase:wine-stable` | Recommended on Linux; `ampbase:wine-stable` | Required trusted ampbase-derived control; Wine is in the separate accepted runtime |
| Wine prefix | Instance `.wine`, `win64`; update removes and reinitializes it | Instance `.wine`, `win64`; update removes and reinitializes it | Instance `.wine`, `win64`; init/update, save-directory setup and optional sound configuration | Persistent `/data/prefix`, `win32`; owned Xvfb and Wine lifecycle, cached VC2015 installation, session files and narrow vendor inputs |
| Stop | `OS_CLOSE`, 30 seconds | `String`, `quit`, 30 seconds; SourceRCON | `OS_CLOSE`, Windows CtrlC override, 30 seconds | `SIGTERM`, 150 seconds at AMP; foreground Docker signal proxy/init; runtime stop budget 120 seconds |
| Ready | Regex from game startup output | RCON connected | Regex from game startup output | Regex from structured supervisor state, with authenticated-current-runtime checks before online lobby readiness |
| Config mapping | INI AutoMap | Template AutoMap to `server.cfg` | KVP AutoMap | JSON AutoMap for gameplay, account input and preset editor; typed helpers apply changes on Start/Restart |
| Ports | UDP 7777 | TCP/UDP 27015 for game/RCON | UDP 7777 and 27015 | Native game TCP 6113; relay and status remain container loopback |
| Declared minimum AMP | 2.4.0.6 | 2.6.0.4 | 2.6.0.4 | Existing declaration 2.5.0.8; a complete version compatibility claim is not established by this number |

These are different application protocols and shutdown contracts. Do not copy
their UDP/RCON ports, Wine architecture, prefix deletion or readiness expressions
into War3. Their update scripts were inspected as source, not executed.

## Lifecycle and readiness conclusions

`SIGTERM` is explicitly listed as a valid Generic exit method in the official
[Generic configuration reference](https://github.com/CubeCoders/AMP/wiki/Configuring-the-%27Generic%27-AMP-module#application).
`OS_CLOSE` sends SIGINT on Linux; `String` sends the configured text through the
application's administrative interface. The official
[Pavlov template at the same pinned revision](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/pavlov-vr.kvp)
also uses `SIGTERM`. Its declared minimum is 2.5.1.8. This supports the enum's
established use; it does not substitute for executing our template on AMP 2.7.

Our Python launcher finishes with `os.execvpe`, retaining the tracked foreground
process and STDIO. Docker uses `--interactive --sig-proxy=true --init`, with no
detach or TTY. `App.MonitorChildProcess=False` fits this design: AMP tracks the
foreground Docker client, while the supervisor owns component recovery. A Wine
child-process name regex from another game would track the wrong layer here.

The control image derives from pinned `cubecoders/ampbase` and preserves its
entrypoint and AMP handoff. This follows the official
[custom-image contract](https://github.com/CubeCoders/AMP/wiki/Using-custom-Docker-images-with-AMP-and-the-generic-module).
The host instance path is recorded separately from `/AMP` for sibling bind
mounts. Only trusted control receives the Docker socket; the game runtime does
not. There is no evidence requiring Wine or Xvfb to move into the control image.

The readiness expression matches a structured `event=state` line with LOBBY,
COUNTDOWN or IN_GAME, not raw native slot telemetry. In `ICCupSupervisor`, LOBBY
and COUNTDOWN follow strict snapshot readiness; authentication, fresh Star/relay
observations, native health and the current native PID/publication must agree.
IN_GAME follows live authentication and native health without requiring a lobby
advertisement. An offline unauthenticated cohort cannot emit these online states
through that controller. The launcher creates a fresh foreground runtime; it
does not replay previous Docker logs as startup output.

AMP's startup-ready indication should still be distinguished from continuous
service health during recovery. Runtime `/status` and its current `ready`,
`iccup_authenticated` and component fields remain authoritative. This review
does not claim that a startup regex automatically demotes AMP's UI state after
a later realm disconnect. The separate offline production supervisor deliberately
permits offline native readiness for tests and is not the online acceptance path.

## Useful template conventions and concrete integration issues

Keep the clear category/subcategory organization, normal enum/number/checkbox
fields, companion manifests, explicit restart semantics and ordinary AMP
Start/Stop/Restart flow. These are the useful patterns shared by the three
official templates. Preserve the separation between lobby creation (`autohost`)
and per-preset automatic countdown (`autostart`).

Two concrete UI issues were already observed by the maintainer and reported to
Goodall; their fixes belong to the AMP implementation owner:

- **P2: preset enum refresh targets the wrong manifest.** The live
  `Meta.ConfigManifest` names `war3-productionconfig.json`, while the reviewed
  helper prefers a `configmanifest.json` alias. Updating the alias can succeed
  on disk without changing AMP's loaded options. Verify the actual named file,
  reload it, and confirm a newly enabled preset ID appears in the live settings
  specification and remains selectable after restart.
- **P2: hidden global autostart controls still appear.** On the installed build,
  the maintainer observed `Hidden` becoming read-only presentation rather than
  removing those fields from the settings specification. Remove the unwanted
  visible manifest entries while preserving internal defaults/backward parsing;
  keep normal operator autostart settings in the preset JSON.

Preset settings use ordinary save plus game Start/Restart. The editor has no
`Core/UpdateApplication` action, because a rejected live preset apply could
mark a still-running game as Error. The compatibility UpdateSources stage uses
`apply-presets --defer-if-running` and leaves active games unchanged. Manifest/UI reload is a separate concern from restarting
the game application; a control-instance restart refreshed the categories in
the maintainer's installed-build test. A template repository Fetch alone is not
proof that an existing instance has loaded its new manifests.

Leave `Meta.MinAMPVersion` unchanged in this documentation-only work. Its current
2.5.0.8 value is not proof that every new UI convenience works on that release;
changing it to 2.7 would not establish compatibility either.

## What is verified and what remains before deployment

The existing historical internal AMP staging evidence (not distributed)
records old accepted rc4 image Start/Restart, graceful component exits, whole
control Stop/Start, preserved SQLite/marker inodes, TCP 6113 only, loopback
relay/status, no runtime Docker socket, no engine execution, and an external
real-client join/map download. These are existing evidence, not new test runs
from this comparison. The runtime is currently stopped while main integrates
the new candidate. UI fields were observed after a control restart on the
installed 2.8.0.10 build.

For the new accepted candidate, main still owns the bounded live verification:
template discovery/managed mode 10; actual named manifest and enum refresh;
ordinary Start/Stop/Restart; fresh authenticated readiness; exactly one Star,
relay and GHost; no engine; TCP 6113 with private loopback endpoints; and retained
profile/prefix/session/SQLite/map settings across restart. This comparison does
not turn old rc4 results into acceptance of the new image or an AMP 2.7 result.

No architecture replacement or extra test framework is justified by this review.
The official repository's [acceptance checklist](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/README.md#after-submitting-a-template)
provides the appropriate baseline: load/update/start/ready/graceful stop/stopped
and applied configuration changes.

## Pinned primary template sources

- Longvinter: [KVP](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/longvinter-wine.kvp), [settings](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/longvinter-wineconfig.json), [updates](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/longvinter-wineupdates.json), [mapping](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/longvinter-winemetaconfig.json), [ports](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/longvinter-wineports.json).
- Contagion: [KVP](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/contagion.kvp), [settings](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/contagionconfig.json), [updates](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/contagionupdates.json), [mapping](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/contagionmetaconfig.json), [ports](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/contagionports.json), [config template](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/contagionserver.cfg).
- ASKA: [KVP](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/aska.kvp), [settings](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/askaconfig.json), [updates](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/askaupdates.json), [mapping](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/askametaconfig.json), [ports](https://github.com/CubeCoders/AMPTemplates/blob/36d02437561729635ca5f07574d7fb58e7c41bfc/askaports.json).

For the version/platform distinction, the official [AMP 2.7 release notes](https://discourse.cubecoders.com/t/amp-deimos-2-7-0-release-notes/38578)
retain Docker support on existing systems while adding Podman support. They do
not require migrating this existing Docker deployment. The [2.7.2 notes](https://discourse.cubecoders.com/t/amp-deimos-2-7-2-release-notes/39817)
also describe container lifecycle/event-stream fixes; version-sensitive behavior
should be recorded against the actual installed build.
