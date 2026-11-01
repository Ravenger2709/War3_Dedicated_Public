# War3 Dedicated — iCCup hostbot

[Русский](#по-русски) · [Third-party credits](ATTRIBUTION.md) · [Security](SECURITY.md)

An open-source, headless Warcraft III 1.26a lobby hostbot for iCCup. It combines a native **GHost++** game host with the user's own **Star** client running under Wine, a Python supervisor and a loopback relay. It can host supported AI maps, replace AI-controlled lobby slots with real players, vote to start and recreate lobbies after games. Only game TCP **6113** is exposed by the production runtime.

**No Warcraft III, Star, maps, account profiles, passwords or private runtime images are included.** You must supply files you are legally permitted to use. Download the tested runtime image and matching release identity from [GitHub Releases](https://github.com/Ravenger2709/War3_Dedicated_Public/releases). The legacy Compose prototype is not the production iCCup runtime.

## Components

| Component | Purpose |
| --- | --- |
| `src/runtime/`, `src/supervisor/`, `src/bridge/` | Process supervision, iCCup Star relay, lobby/rehost state and health reporting |
| `patches/` + `scripts/build-ghost.sh` | Explicit GHost++ upstream revision and compatibility/gameplay patch |
| `docker/production/` | Multi-stage source-only runtime image packaging, without licensed assets |
| `scripts/war3.py` | Standalone Docker management: setup, account, start, stop, restart, status, logs |
| `amp/war3-production*` | Optional AMP Generic template and instance settings |
| `config/presets.example.json` | Example map preset including start/vote thresholds and optional AI approval |

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

## Provenance and rights

The native host is based on [GHost++](https://github.com/ghostBot/ghostplusplus), originally by **Trevor Hogan**, with important bundled dependencies credited separately. The compatibility/gameplay modifications are published as a patch against a pinned upstream commit. The design was informed by public [iCCup](https://iccup.com/dota/files.html), [PvPGN](https://github.com/pvpgn/pvpgn-server), [unmclient](https://github.com/mothefuunnick/unmclient), [IrInA-related sources](https://github.com/kirill-782/irinabot-ru-site) and the official [CubeCoders AMPTemplates](https://github.com/CubeCoders/AMPTemplates). Research references are not claims that their proprietary files or code have been incorporated. See [ATTRIBUTION.md](ATTRIBUTION.md) and [src/hostbot/NOTICE.md](src/hostbot/NOTICE.md) for author, license and redistribution details.

The project's original integration code is provided under the [Apache License 2.0](LICENSE); upstream components retain their respective terms. **This is an independent project**, not endorsed by Blizzard Entertainment, iCCup or CubeCoders.

## По-русски

Этот репозиторий содержит исходники хост-бота Warcraft III для iCCup. Можно использовать Docker без AMP или подключить Generic-шаблон AMP. Для запуска нужны собственные файлы Star/Warcraft III, карта, отдельный iCCup-аккаунт и собранный/проверенный runtime-образ. Приватные конфигурации не публикуются; готовый образ и данные проверки доступны в GitHub Releases. Параметры автостарта, голосования и ИИ редактируются в [пресетах](docs/map-presets.md); инструкции по [Docker](docs/standalone.md) и [AMP](docs/operator-quickstart.md) доступны в репозитории.
