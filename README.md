# War3 Dedicated — iCCup hostbot

[Русский](#по-русски) · [Third-party credits](ATTRIBUTION.md) · [Security](SECURITY.md)

An open-source, headless Warcraft III 1.26a lobby hostbot for iCCup. It combines a native **GHost++** game host with the user's own **Star** client running under Wine, a Python supervisor and a loopback relay. It can host supported AI maps, replace AI-controlled lobby slots with real players, vote to start and recreate lobbies after games. Only game TCP **6113** is exposed by the production runtime.

**No Warcraft III, Star, maps, account profiles, passwords or private runtime images are included.** You must supply files you are legally permitted to use. Download the tested runtime image and matching release identity from [GitHub Releases](https://github.com/Ravenger2709/War3_Dedicated_Public/releases). The legacy Compose prototype is not the production iCCup runtime.


## Official Star download and minimal game files

**Official iCCup BETA Star Launcher:** [download page](https://iccup.com/files/view/iCCup_BETA_Star_Launcher.html) ([iCCup file directory](https://iccup.com/files.html)). Use **BETA Star**, not the older classic iCCup Launcher. The verified runtime requires **Star 4.3.75.75** and checks the exact 31 vendor files listed in [config/star-vendor-files.json](config/star-vendor-files.json). The public download may offer a newer version: do not bypass version/SHA checks; a newer Star must be tested and approved before use.

**You do not have to install a full Warcraft III game on the AMP server.** The runtime Docker image already contains native GHost++, Wine, Xvfb and the bot. It intentionally does **not** ship the third-party Star files or Warcraft assets. Provide these sources **once during setup**:

| Setup option | Source files to supply | Used after setup |
| --- | --- | --- |
| `--star-dir` | Folder with the extracted/updated official Star installation and `Launcher.exe` (not the downloaded installer executable) | Only 31 verified Star files are copied into private `vendor/star`; Star runs headlessly via Wine/Xvfb |
| `--game-dir` | A folder containing `war3.exe`, `game.dll`, and `War3Patch.mpq` from your licensed Warcraft III **1.26a** | EXE/DLL are read-only metadata and never executed; MPQ supplies map-checksum data |
| `--map` | Your permitted `.w3x` or `.w3m` map | The native bot hosts and transfers it to players |
| `--redist` | Compatible VC2015 x86 redistributable installer (can be found under supported Star paths) | Used for the Wine runtime; setup validates its pinned SHA-256 |

Setup copies just the allowlisted files into the **private instance data directories** and mounts them into the container. The original source folders are not needed for normal Start/Stop/Restart, but keep authorized backups for disaster recovery. **No Warcraft game engine or graphical game client runs on AMP**; the headless Star service is separate from the actual Warcraft III game.

See [AMP one-time setup](docs/operator-quickstart.md#required-third-party-files-one-time-setup) and [standalone setup](docs/standalone.md#set-up-and-enter-the-account).


## Components

| Component | Purpose |
| --- | --- |
| `src/runtime/`, `src/supervisor/`, `src/bridge/` | Process supervision, iCCup Star relay, lobby/rehost state and health reporting |
| `patches/` + `scripts/build-ghost.sh` | Explicit GHost++ upstream revision and compatibility/gameplay patch |
| `docker/production/` | Multi-stage source-only runtime image packaging, without licensed assets |
| `scripts/war3.py` | Standalone Docker management: setup, account, start, stop, restart, status, logs |
| `amp/war3-production*` | Optional AMP Generic template and instance settings |
| `config/presets.example.json` | Readable map presets with start/vote thresholds and computer settings |

No external database server is required; runtime state uses local SQLite. Wine and its dependencies run inside Docker, not on the host. AMP is **optional** and has its own Docker control image.

## Installation and development

1. Download the release image and its identity as described in [standalone setup](docs/standalone.md), or read [building a runtime from source](docs/build-from-source.md).
2. For a verified immutable runtime plus corresponding trusted release identity, use [Docker standalone setup](docs/standalone.md) (Linux amd64, Docker, Python 3.10+) or the [AMP operator guide](docs/operator-quickstart.md).
3. Supply your own licensed Warcraft III 1.26a and Star files, map and iCCup account via the private setup process. Account passwords are entered interactively or into AMP's private instance settings and must never be committed.
4. For maps with verified AI support, edit `data/config/presets.json` and select the desired preset. See [presets](docs/map-presets.md) and [account setup](docs/account-setup.md).

With a properly prepared and accepted standalone instance:

```bash
python3 scripts/war3.py account
python3 scripts/war3.py start
python3 scripts/war3.py status
python3 scripts/war3.py logs --tail 100
python3 scripts/war3.py stop
```

Automated tests are in `tests/`; see [source build and validation](docs/build-from-source.md). Development and diagnostics do not require any real credentials. Never use the old legacy/offline container as a production iCCup runtime.

## Map Cycle development candidate

The Phase 4 source adds an ordered `map_cycle`, per-map settings and up to five
loading/running matches in one GHost process, with one Star/relay session and
one public lobby. AMP is optional; standalone uses the same settings and runtime.
The cursor advances on a durable native start, and active connections survive
the next lobby opening on TCP 6113. See [map presets](docs/map-presets.md).

These changes require a matching accepted runtime image and updated controller;
the previously released v0.4.1 image does not certify Phase 4. Native/offline
integration tests have passed, but real parallel Warcraft III clients, live
iCCup rotation and deployment acceptance remain pending. See the
[Phase 4 evidence checklist](docs/phase4-acceptance-status.md) before rollout.
Unknown-crash ownership or unresolved cycle state blocks new hosting until
reconciliation; it must not be erased to force a restart.

## AI Director design proposal

The [AI Director implementation plan](docs/design/ai-director-plan.md) studies
an optional model service, AMP/standalone settings and a reusable map SDK.
It includes an [existing-project review](docs/design/ai-director-research.md)
and a [candidate classic transport protocol](docs/design/ai-director-protocol.md).

**Design only:** no AI service, new AMP controls or map bridge are implemented
by this documentation branch. Stock Warcraft III 1.26a transport, unchanged
player capacity and multiplayer/replay correctness require a native-client
proof before implementation proceeds.

## Provenance and rights

The native host is based on [GHost++](https://github.com/ghostBot/ghostplusplus), originally by **Trevor Hogan**, with important bundled dependencies credited separately. The compatibility/gameplay modifications are published as a patch against a pinned upstream commit. The design was informed by public [iCCup](https://iccup.com/dota/files.html), [PvPGN](https://github.com/pvpgn/pvpgn-server), [unmclient](https://github.com/mothefuunnick/unmclient), [IrInA-related sources](https://github.com/kirill-782/irinabot-ru-site) and the official [CubeCoders AMPTemplates](https://github.com/CubeCoders/AMPTemplates). Research references are not claims that their proprietary files or code have been incorporated. See [ATTRIBUTION.md](ATTRIBUTION.md) and [src/hostbot/NOTICE.md](src/hostbot/NOTICE.md) for author, license and redistribution details.

The project's original integration code is provided under the [Apache License 2.0](LICENSE); upstream components retain their respective terms. **This is an independent project**, not endorsed by Blizzard Entertainment, iCCup or CubeCoders.

## По-русски

**Где скачать Star:** [официальная страница iCCup БЕТА Star Launcher](https://iccup.com/files/view/iCCup_BETA_Star_Launcher.html). Нужен именно Star, а не классический iCCup Launcher. Проверенная версия — **4.3.75.75**; более новую версию необходимо отдельно проверить на совместимость.

**Полный Warcraft III на AMP устанавливать не нужно.** Для первоначального setup требуются файлы `war3.exe`, `game.dll`, `War3Patch.mpq` из вашей Warcraft III 1.26a, выбранная карта `.w3x`/`.w3m`, каталог распакованного Star и VC2015 x86. Настройка копирует только нужные файлы в закрытые данные инстанса. Docker уже содержит GHost++, Wine и Xvfb, а игровой движок не запускается. [Подробная инструкция для AMP](docs/operator-quickstart.md#required-third-party-files-one-time-setup).


Этот репозиторий содержит исходники хост-бота Warcraft III для iCCup. Можно использовать Docker без AMP или подключить Generic-шаблон AMP. Для запуска нужны собственные файлы Star/Warcraft III, карта, отдельный iCCup-аккаунт и собранный/проверенный runtime-образ. Приватные конфигурации не публикуются; готовый образ и данные проверки доступны в GitHub Releases. Параметры автостарта, голосования и ИИ редактируются в [пресетах](docs/map-presets.md); инструкции по [Docker](docs/standalone.md) и [AMP](docs/operator-quickstart.md) доступны в репозитории.

## G1 experiment (development branch)

The initial classic 1.26a map/action-bridge spike is documented in [the G1 procedure](docs/design/ai-director-g1.md). It is disabled by default, uses no LLM and has not passed the real-client/desync gate. No AMP or production deployment is part of this experiment.
