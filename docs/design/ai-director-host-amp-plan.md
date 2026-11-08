# AI Director: интеграция с хост-ботом и AMP

**DESIGN ONLY — ничего из описанного AI-модуля ещё не реализовано; OpenAI/API других моделей не вызывались.** Документ фиксирует аудит и план, а не готовность игрового решения. Дата: 2026-11-08. Целевой клиент: Warcraft III: The Frozen Throne **1.26a / 1.26.0.6401**.

## 1. Зафиксированная база

Аудит выполнен для `Ravenger2709/War3_Dedicated_Public`, `main`, commit `34c1c734a6f04b2537001b7cb734de8a4e3e902e`. GHost++ закреплён на upstream `6ba90f426e96f80fe013a468d1e07151a510adbe`; изменения хоста поставляются через `patches/ghostplusplus-modern-linux.patch` и digest в `third_party/ghost.lock.json`. [H1–H3]

Текущий runtime запускает native GHost++, Python supervisor, Star worker и relay. Игровой движок отсутствует: `runtime-contract.json` запрещает выполнение `war3.exe` и `game.dll`. Поэтому у GHost нет самостоятельного снимка здоровья, координат, инвентаря или целей юнитов. Существующий `presets.ai` управляет компьютерными строками лобби; поведение этих компьютеров исполняют карта и Warcraft-клиенты. [H1, H2, H5]

Source Phase 4 предусматривает один публичный lobby и до пяти loading/running matches. Документация отдельно оставляет pending реальные параллельные Warcraft-клиенты, live iCCup rotation и окончательную приёмку образа/AMP. Эти исходники нельзя считать доказательством готовности нового AI. [H4]

## 2. Выбранная граница сервиса

Предлагается независимый Python worker `src/ai_service/`, первоначально внутри существующего runtime image, под управлением supervisor. Он получает нормализованные наблюдения, вызывает выбранный provider, проверяет ответ, применяет бюджет и отправляет ограниченные намерения обратно в native bridge. Игровой JASS проверяет актуальность и допустимость намерений, а затем исполняет их.

Это позволяет управлять модулем через существующий AMP Generic template и тем же standalone CLI. Отдельный container/общий сервис нескольких инстансов остаётся последующим вариантом: интерфейс worker не должен зависеть от упаковки. Для MVP отдельный процесс уменьшает число новых владельцев lifecycle и не требует дополнительного публичного порта.

LLM принимает стратегические решения; локальный AI карты продолжает движение, бой, лечение и аварийные реакции во время ожидания. API запрещено блокировать в GHost event loop. Ответ модели никогда не становится произвольным JASS, shell command или raw action payload.

## 3. Transport: сначала доказательство на 1.26a

Upstream `CGame::EventPlayerAction` вызывает statistics parser; `CStatsW3MMD::ProcessAction` читает gamecache sync opcode `0x6B` и namespace `MMD.Dat`. Это подтверждает направление карта → host для специально подготовленных данных, но не готовую двустороннюю шину AI. Статистический parser не должен становиться механизмом доверия: в нём имеется незавершённая проверка check messages. [U1, U2]

Исследуемый вариант W3HMC предполагает `0x6B` uplink и `0x60` downlink через синхронные игровые actions. Это **кандидат для PoC**, а не гарантия совместимости данного хоста и клиента. До выбора wire format нужны тестовая карта, два настоящих 1.26a клиента, получение команды JASS, ACK и replay. Точный PID, управление sequence и ограничения payload устанавливаются этим экспериментом. [Протокольный план](ai-director-protocol.md) отдельно разбирает два receiver identity fields, сохранение slot budget, недоверенный uplink и scoped reassembly.

Native decoder следует подключить независимо от `map_type=w3mmd`, чтобы сохранить существующие Dota/W3MMD parsers. Обратные команды должны проходить единый native action/replay path. `SendAllChat()`, поллинг SQLite статистики и stdin GHost не являются готовым RPC: GHost не читает административные команды из stdin. [H3, U1, U3]

При отсутствии доказанного канала реализуются только transport spike и shadow-наблюдение. Поддержка неизменённой произвольной карты не заявляется. Универсальность означает общий SDK/protocol и отдельный адаптер карты; материалы Afterlife остаются в [приватном war3-maps](https://github.com/Ravenger2709/war3-maps).

## 4. Изоляция отказов supervisor

Есть конкретная опасность в `src/runtime/controller.py`: цикл `ICCupSupervisor.run()` относит смерть любого child, кроме native, к отказу realm; `_failure()` вызывает `_stop_realm()`, останавливая Star и relay. Простое добавление AI в `self.children` нарушит изоляцию. [H6]

Нужны отдельные optional-component handlers для AI, собственные generation, restart budget, backoff и circuit breaker. Ошибка провайдера, 429, timeout, malformed JSON или смерть worker должны означать `ai_service.degraded` и локальный fallback карты. Действующий hosting остаётся работоспособным; Star, lobby и матчи не перезапускаются из-за AI.

Отключение отменяет pending jobs и запрещает новые API запросы/DECISION injections; priority RELEASE_CONTROL и lifecycle frames остаются разрешены. Если release потерян, finite game-time lease возвращает local AI; во время pause wall-clock SLA не обещается. Завершение матча удаляет только его очередь. Смена native generation аннулирует все старые решения. При останове всего runtime сначала прекращается приём новых AI задач, затем выполняется штатный graceful stop. Лимиты очередей и IPC фиксируются, чтобы медленная модель не создавала неограниченный backlog.

## 5. Идентичность матчей и immutable inputs

Для каждого сообщения необходимы `session_generation`, `host_counter`, durable `match_id`, `map_sha256`, `adapter_id`, trusted control_scope_id с allowed actors/observation audience, protocol version, observation sequence, decision sequence и deadline в согласованном игровом времени. Имена lobby/player/preset сами по себе недостаточны: одинаковый preset может идти одновременно в нескольких матчах.

Текущие `matches_v1`, lifecycle journal и startup catalog уже дают необходимые идентификаторы и связь с sealed map bytes. Отдельно закрепляются adapter revision, effective policy/config revision и model profile на начало матча. Смена настроек следующего lobby не переписывает активные матчевые контексты. [H7]

Native и карта отвергают другой scope, старое поколение, дубликаты, просроченные решения и неизвестные capabilities. Допустимость цели, ресурсы, cooldown, владение юнитом и текущий режим проверяет карта перед действием. Доставленные решения фиксируются в replay; проигрывание записи не вызывает провайдер повторно.

## 6. Конфигурация: единые имена

Глобальный namespace — **`ai_service`**, per-preset — **`external_ai`**. Существующий `ai.enabled/target/skill` сохраняет смысл computer slots. Канонический [пример конфигурации](ai-director-config.example.json) содержит все выбранные defaults и явно отмечен как design-only. Его пока не принимают существующие runtime validators.

| Параметр | Начальный профиль |
| --- | --- |
| enabled / mode | false / off |
| Provider / model | openai_responses / свободный ID после preflight |
| Cadence / HTTP deadline | 10s / 4 wall секунды |
| Max snapshot age / lease | 6 / 15 игровых секунд от snapshot |
| Max input / output tokens | 4096 / 600 |
| In-flight | 1 на control scope, 2 на instance |
| Общие request/token caps | 12 RPM / 500000 total tokens/hour |

Значения интервала и лимитов — стартовые проектные значения для измерений, не доказанный баланс Afterlife. `external_ai` выбирает enabled/mode, adapter и разрешённый profile конкретного preset; ключ провайдера туда не попадает. Instance budgets действуют для всех scopes суммарно. Global enabled=false или mode=off дают effective off; preset/runtime не повышает режим выше global cap off < shadow < active и не увеличивает request/token/concurrency caps. Preflight проверяет ключ только если он обязателен выбранному provider.

Model ID — свободное поле. Provider adapter проверяет поддерживаемый API, параметры и формат ответа; присутствие модели в списке аккаунта не доказывает поддержку нужного structured output. Политика несовместимости должна давать понятную ошибку конфигурации AI и fallback, не угадывать незадокументированные параметры.

Текущие parsers строго отвергают неизвестные ключи. Поэтому конфигурация потребует согласованных изменений в `src/supervisor/config.py`, runtime validators, `src/runtime/presets.py` и `amp/production.py`, с миграцией старых файлов без включения сервиса. [H5, H6, H8]

## 7. AMP UI и standalone

В существующем шаблоне `Meta.ConfigManifest=war3-productionconfig.json`, companion metaconfig делает JSON AutoMap. Уже применяются checkbox, enum, number, text, password и Textarea; новый AMP plugin для этих полей не обязателен. Официальные CubeCoders templates подтверждают этот механизм. [H9, A1]

Категория External AI должна предоставлять включение, mode, provider, base URL, model ID, ввод/ссылку на secret, интервал, timeout, output token cap, concurrency, budgets и per-map profiles. Не следует помещать десятки технических protocol fields в основной операторский поток.

**MVP применяет конфигурацию на Start/Restart.** Сейчас `App.SupportsLiveSettingsChanges=False`; изменение этого флага само по себе не реализует hot apply. После G1 в последующем optional live disable возможен через проверенную native/supervisor management command и отдельное действие UI, с ACK, отменой задач и сохранением матчей. Нельзя заставлять операторскую кнопку незаметно перезапускать активные игры.

Standalone использует те же validators и итоговый `supervisor.json`; `scripts/war3.py` получает эквивалентные configure/check/status команды. Совпадение semantics проверяется тестом. Название существующего `amp-settings.json` не означает обязательность установленного AMP. [H5]

## 8. Secrets, IPC и наблюдаемость

Worker получает ключ через отдельный read-only secret file; API key не хранится в карте, публичном preset, argv, status или prompt. Для AMP возможен masked input с переносом в приватный файл, но маскировка UI не гарантирует шифрование хранения: private AMP settings могут сохранять введённое значение. Очистка/замена ключа должна иметь явную семантику.

IPC — приватный Unix socket с ограничениями владельца, payload и rate. Контракт проверяет источник и матч независимо от содержимого LLM ответа. Endpoint требует TLS; исключения для operator-selected локального inference endpoint задаются явно. Пользовательские строки карты считаются данными, а не инструкциями провайдера.

В `/status` нужны mode, configured/ready/degraded, model profile, возраст последнего наблюдения, backlog, counters timeout/reject/fallback и usage. Логи ограничены по размеру и редактируют secrets. Текущая документация не подтверждает stock AMP канал произвольных per-match graphs: для MVP достаточно status и понятных console lines. [H7]

## 9. Файлы интеграции и упаковка

| Файл/каталог | Планируемая ответственность |
|---|---|
| `src/ai_service/` | Worker, provider adapters, scheduler, budgets, validation, IPC |
| `src/runtime/controller.py` | Независимое восстановление AI и scope матчей |
| `src/runtime/native.py`, native patch | Bridge configuration и action transport |
| `src/runtime/presets.py`, preload catalog/manifest/compiler | `external_ai`, map/adapter pins |
| `amp/production.py`, `war3-productionconfig.json`, metaconfig | Проверка, UI mapping, private inputs |
| `scripts/war3.py` | Standalone эквивалент |
| `docker/production/`, `third_party/ghost.lock.json` | Образ, source contract, patch digest |
| Secret audit, public-tree checks | Исключение keys, private map artifacts и payload dumps |

Предпочтительный новый runtime image публикуется в **GHCR** с immutable digest и соответствующей release identity. Это будущий канал поставки; текущая публичная документация описывает release download, и его миграция требует явного обновления инструкций. Образ не включает Warcraft/Star assets или приватные карты. Рабочие сборки и evidence после публикации сжимаются/очищаются по правилам проекта, без удаления незапушенных материалов.

## 10. Этапы и критерии приёмки

1. **Transport gate:** минимальная карта, фиксированный response, два настоящих 1.26a клиента, ACK, отсутствие desync, replay и поведение при паузе/лагах. До успеха не объявлять active mode готовым.
2. **Worker без LLM:** deterministic provider; duplicate/stale/wrong-match rejection, bounded queues, worker crash. Hosting и action relay продолжаются.
3. **Provider + shadow:** реальные authorized API проверки отдельно, измерение latency/usage, malformed/refusal/429/timeout и cost limits.
4. **Первый private adapter:** Afterlife acceptance в приватном репозитории; публичный хост содержит только общий контракт.
5. **AMP/standalone:** disabled по умолчанию; отсутствие запросов/дочернего worker при disabled; одинаковые настройки; restart и secrets persistence.
6. **Multi-match:** две одновременные карты/сессии, законченный матч не получает поздний ответ, следующий lobby не наследует очередь.
7. **Release:** source checks, необходимые native/integration gates, секреты/образ, GHCR digest, rollback на предыдущий accepted runtime.

Synthetic W3GS peers доказывают маршрутизацию, но не JASS исполнение или совместимость движка. Existing Phase 4 evidence не переносится автоматически на изменённый native patch и новый runtime image.

## Источники

Все H-ссылки закреплены на audited host commit.

- [H1 — README](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/README.md).
- [H2 — runtime contract](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/docker/production/runtime-contract.json).
- [H3 — host interface](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/src/hostbot/INTERFACE.md).
- [H4 — Phase 4 acceptance](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/docs/phase4-acceptance-status.md).
- [H5 — presets](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/src/runtime/presets.py).
- [H6 — controller](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/src/runtime/controller.py).
- [H7 — match telemetry](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/docs/native-match-telemetry.md).
- [H8 — AMP settings/controller](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/amp/production.py).
- [H9 — Generic template](https://github.com/Ravenger2709/War3_Dedicated_Public/blob/34c1c734a6f04b2537001b7cb734de8a4e3e902e/amp/war3-production.kvp).
- [U1 — upstream game actions](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/game.cpp).
- [U2 — upstream W3MMD parser](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/statsw3mmd.cpp).
- [U3 — upstream action/replay queue](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/game_base.cpp).
- [A1 — CubeCoders JSON AutoMap example](https://github.com/CubeCoders/AMPTemplates/blob/93b03cca380ee2a9ee35577a155b485f7227abd9/openstarboundmetaconfig.json) и [settings manifest](https://github.com/CubeCoders/AMPTemplates/blob/93b03cca380ee2a9ee35577a155b485f7227abd9/openstarboundconfig.json).
