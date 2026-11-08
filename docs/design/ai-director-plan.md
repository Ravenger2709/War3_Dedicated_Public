# AI Director для Warcraft III 1.26a: план внедрения

Дата: 2026-11-08. Статус: **DESIGN ONLY / transport feasibility pending**.

Этот документ описывает предлагаемый сервис. В исследовательской ветке нет работающего AI worker, native bridge, нового AMP UI или изменённых игровых карт. Вызовы платного API, запуск Warcraft и production deployment в рамках подготовки плана не выполняются.

> Обновление G1 от 2026-11-08: в feature-ветке реализован и проверен одним
> реальным клиентом [контрольный двусторонний мост](ai-director-g1.md).
> Он занимает один человеческий слот (11 реальных игроков); это не завершает
> требование сохранения всех 12 мест. Остальная архитектура ниже остаётся
> проектом: LLM/worker/AMP-интеграция не реализованы и не включены.

## Решение

Добавить в текущий хост-бот опциональный **AI Director**: отдельный Python-процесс, который получает ограниченные наблюдения из карты, обращается к выбранной модели и возвращает проверяемые намерения ботам. Быстрый бой, движение, выживание и исполнение приказов остаются в детерминированном AI карты. Первый адаптер — Afterlife; общий протокол и сервис не должны импортировать код конкретной карты.

Для первой версии worker входит в существующий runtime image и управляется supervisor. Позже тот же процесс можно вынести в sidecar. AMP остаётся необязательным: UI и standalone используют одну конфигурационную модель.

**Первый этап разработки — доказать двусторонний канал на настоящих клиентах 1.26a с сохранением вместимости карты.** До этого полноценная интеграция с моделью не считается реализуемой и готовой к выпуску.

Документы:

- [Готовые проекты и основания выбора](ai-director-research.md).
- [Протокол, синхронизация и проверка транспорта](ai-director-protocol.md).
- [Точки интеграции хост-бота и AMP](ai-director-host-amp-plan.md).
- [Предлагаемая конфигурация](ai-director-config.example.json) — дизайн, существующие validators её пока не принимают.
- Map SDK и адаптеры находятся в приватном [war3-maps](https://github.com/Ravenger2709/war3-maps), в ветке research/ai-director-api-20261108.

## 1. Что установлено по исходникам

Аудит хост-бота закреплён на 34c1c734a6f04b2537001b7cb734de8a4e3e902e. Основные источники — [README](../../README.md), [runtime contract](../../docker/production/runtime-contract.json), [controller](../../src/runtime/controller.py), [native lifecycle](../contracts/phase4-native-lifecycle.md), [preset contract](../map-presets.md), [AMP manifest](../../amp/war3-productionconfig.json).

| Факт | Следствие |
| --- | --- |
| Runtime исполняет GHost++, Star/Wine и supervisor; игровой движок не запускается | Состояние мира должно поступать из Warcraft-клиентов через карту |
| W3MMD в закреплённом GHost++ читает gamecache sync actions | Есть основание для uplink, но нет готового AI RPC |
| Native host пересылает actions всем клиентам и сохраняет их в replay | Синхронный downlink следует вводить в этот же путь |
| Source Map Cycle содержит один lobby и несколько running matches | Решение всегда принадлежит точному match scope |
| AMP Generic уже записывает JSON settings | Переключатель и поля модели можно добавить в существующее приложение |
| App.SupportsLiveSettingsChanges=False | Сохранение поля само по себе не меняет текущий матч |
| presets.ai управляет computer-слотами лобби | Новый модуль получает отдельные ai_service / external_ai |
| Отказ неизвестного child попадает в realm recovery | AI требует собственного recovery, иначе возможен перезапуск Star |

Source capability не равна игровой приёмке. В [Phase 4 acceptance](../phase4-acceptance-status.md) реальные параллельные клиенты и live iCCup acceptance ещё выделены как незавершённые.

Для headless host по-прежнему нужен хотя бы один реальный Warcraft-клиент, исполняющий матч. Fake player, LLM и GHost++ не заменяют движок. Полностью автономная симуляция матча без клиентов — отдельный проект с игровым runtime и не входит в этот план.

## 2. Границы универсальности

«Подключить карту» означает установить общий SDK в совместимый JASS и описать адаптер карты: сущности, команды, ресурсы, наблюдения, игровые фазы и резервное поведение. Для уже подготовленной карты оператор выбирает адаптер/профиль в preset; для новой карты разработчик один раз делает этот адаптер.

Неизменённая произвольная .w3x не предоставляет универсального семантического API. Защищённые карты с неизвестными триггерами, карты без разрешённого изменения и клиенты с другими protocol assumptions требуют отдельной оценки. Устанавливать DLL на машины игроков или повышать клиент до 1.27 ради первого MVP не предполагается.

Общий SDK содержит transport framing, очереди, дедупликацию, leases, logical IDs и ACK. Map adapter определяет, что значит «лечиться», «купить», «защищать», какие данные видны и какой AI продолжает работу без сервиса.

## 3. Архитектура

~~~mermaid
flowchart TD
    A["AMP или standalone config"] --> B["Runtime supervisor"]
    B --> C["AI Director worker"]
    B --> D["GHost++ bridge"]
    C <--> E["OpenAI или совместимый endpoint"]
    C <--> D
    D <--> F["Map SDK в синхронном JASS"]
    F --> G["Адаптер карты"]
    G --> H["Локальный AI и исполнение"]
    H --> F
~~~

| Компонент | Ответственность | Предлагаемое расположение |
| --- | --- | --- |
| Native bridge | Ограниченный декодер, match routing, очередь синхронных команд, replay | Existing GHost patch + отдельные native source units |
| AI service | Scheduling, providers, бюджеты, validation, наблюдаемость | src/ai_service/ |
| Local IPC | Version handshake и bounded messages без публичного порта | Private Unix socket |
| Map SDK | Детерминированное получение/проверка команд и отправка наблюдений | Общие tools и SDK в war3-maps |
| Map adapter | Смысл целей, capability list, owner arbitration, fallback | maps/<slug>/tools/ |
| Operator integration | Settings, secrets, health, enable/disable | Existing AMP/standalone controllers |

HTTP-запрос не выполняется в native event loop и не блокирует JASS. Worker не получает прав останавливать лобби, менять аккаунт, запускать shell или передавать произвольные native action bytes. Карта и provider не задают адрес назначения сетевых запросов.

## 4. Два уровня AI

Локальный контроллер продолжает двигаться, атаковать, проверять угрозы, применять экстренное лечение и завершать начатые действия независимо от сети. Его частоту задаёт карта; для первого адаптера существующий быстрый цикл сохраняется.

LLM получает снимок раз в несколько секунд или по значимому событию: новая фаза, смерть/возвращение союзника, исчезновение цели, важный лут, смена роли. Ответ — ограниченный план с TTL. Примеры: исследовать доступную область; прикрыть союзника; отойти в безопасную область; повысить приоритет боеприпасов; сохранить ресурс для способности.

Базовый приоритет исполнения:

1. Lifecycle и запрет управления человеком.
2. Защищённое окно уже принятого действия по контракту адаптера: каст, предмет, выстрел, переход.
3. Экстренное выживание. Прерывание защищённого окна допустимо только для действия с явно разрешённой отменой.
4. Актуальное внешнее намерение.
5. Обычный локальный план.

LLM не выдаёт приказ движения каждый кадр. Отдельный второй таймер не должен бесконечно перезаписывать команды существующего AI. Один arbiter карты владеет приказами для каждого actor.

Режимы:

- **off**: worker не запускается, ключ не требуется, запросов API нет, bridge не активируется.
- **shadow**: решения запрашиваются и оцениваются, но не применяются к игре.
- **active**: разрешённые намерения исполняются после повторной проверки в карте.

Падение worker, timeout, 429, невалидный JSON, отсутствие обязательного для выбранного provider ключа, устаревшая цель и исчерпание бюджета переводят AI в локальное управление. Хостинг при этом продолжается.

## 5. Выбор модели

Для OpenAI основной adapter использует Responses API и ограниченный структурированный результат. Для других серверов нужен отдельный adapter OpenAI-compatible Chat Completions; совпадение URL/формы запроса ещё не доказывает поддержку всех возможностей.

**Model ID — свободная строка**, без списка из двух заранее зашитых моделей. Предлагается отдельная операция model-list через GET /v1/models, затем capability preflight выбранной модели. Список моделей сообщает доступность, но не гарантирует поддержку схемы, параметров или нужную задержку.

Проверять: endpoint, право вызова, нужный текстовый интерфейс, structured output либо проверяемый JSON, разрешённые параметры, лимит ответа и фактическую задержку. Несовместимая модель показывает понятную ошибку и оставляет local AI. Нельзя незаметно подменять её более дорогой моделью.

Для строгих моделей применяются Structured Outputs/strict function arguments. Для JSON-only — локальный schema validator; неполный, refused или ошибочный ответ не исполняется. Произвольный текст никогда не превращается в код. Наличие JSON Schema не подтверждает допустимость игровой команды: ownership, цена, cooldown, видимость и актуальность повторно проверяются локально.

Provider parameters передаются только через его capability profile: max_output_tokens и max_completion_tokens не считаются взаимозаменяемыми; unsupported temperature/reasoning fields не отправляются. Все streaming fragments накапливаются до завершённого валидного плана.

Официальные источники: [Responses](https://developers.openai.com/api/reference/responses/overview), [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses), [Models list](https://developers.openai.com/api/reference/resources/models/methods/list). Численные цены и доступность конкретного аккаунта этим планом не утверждаются.

Локальная модель может использовать тот же provider adapter через отдельно настроенный endpoint. Установка модели и GPU inference в host image не требуются.

## 6. Частота, нагрузка и расходы

Начальные значения — параметры эксперимента, не измеренные характеристики:

| Параметр | Начальное значение |
| --- | --- |
| Одна стратегия на team scope | Каждые 10 секунд, события с debounce |
| Одновременные запросы | 1 на scope, до 2 на instance |
| HTTP deadline | 4 секунды |
| Max accepted observation age | 6 игровых секунд |
| Plan lease | До 15 игровых секунд от snapshot_tick; локальная проверка актуальности на каждом шаге |
| Max output tokens | 600 на один team plan |
| Очередь | Только последний snapshot каждого scope |
| Requests/minute | 12 на instance, включая разрешённые повторы |
| Tokens/hour | 500000 на instance, включая все scopes; настраивается |
| Fallback | Local AI без блокировки матча |

Для active mode дополнительно предложены компактные caps: snapshot до 8×96=768 bytes, decision до 2×96=192 bytes. Это стартовые значения для G1. Допуск учитывает весь путь: uplink + очередь + provider deadline + downlink + map tick margin; при нехватке оставшегося срока запрос отменяется до API либо ответ отклоняется до injection. Абсолютные framing ceilings не разрешают отправлять устаревающие большие планы. Подробности и единицы времени — в протоколе.

При одном запросе каждые 10 секунд получается 360 запросов/час на scope. При условных 1500 input + 500 output это 720000 токенов/час до учёта caching; это арифметический пример, а не замер Afterlife. С ограничением 500000 сервис должен снизить частоту или перейти на fallback. Для восьми независимых запросов каждые две секунды было бы 14400 запросов/час, поэтому такой режим не выбирается.

Один team request может возвращать разные намерения нескольким ботам. Это объединение API-вызовов, а не принудительное движение всех ботов одной группой. Для противоборствующих команд контекст и бюджеты разделены через trusted control_scope_id; скрытые данные другой команды в prompt не попадают. Instance limits остаются общим потолком для всех scopes.

Стоимость рассчитывается из фактических usage input/cached/output и актуальной таблицы цен модели. Token budget считает все input+output, включая cached input; caching может изменить денежную цену, но не вычитает токены из этого счётчика. Если цена неизвестна, UI показывает unknown; лимиты запросов/токенов работают независимо. Денежный cap активируется только при заданном актуальном price profile.

Перед dispatch резервируется худший допустимый расход input+max_output для всех in-flight запросов; после ответа производится reconciliation. Учёт переживает перезапуск. HTTP timeout не означает, что provider не обработал запрос: расход остаётся uncertain/reserved до консервативного списания; слепой retry запрещён. Rate-limit backoff не удерживает игровой tick.

## 7. AMP, config и выключение

Подробности — в [host/AMP плане](ai-director-host-amp-plan.md). Требуемые настройки:

- Global enabled и mode; provider/API format, endpoint, model ID и secret-file reference.
- Частота, deadline, response limit, concurrency, request/token budget.
- Per-preset adapter ID, policy profile, mode и при необходимости другая модель.
- AI health, latency, rejected/stale decisions, применённые ACK, расход и причина fallback через /status и console. Отдельные произвольные AMP widgets/graphs не заявлены.

Precedence: безопасные defaults → instance ai_service → разрешённые preset external_ai overrides → явная runtime override с номером config revision. Global enabled=false или global mode=off дают effective off. Уровни off < shadow < active: preset/runtime override не поднимает режим выше global cap и не увеличивает instance request/token/concurrency limits. Повышение global cap для активного матча — только отдельное проверенное control действие с config revision/handshake. Ключи и произвольные endpoint overrides из map не принимаются.

MVP использует штатное применение настроек на Start/Restart. После G1 отдельной возможностью проектируется runtime disable/status через проверенную private supervisor command; текущее приложение этого не умеет. Отключение запрещает новые DECISION injections, отменяет jobs, но пропускает priority RELEASE_CONTROL/lifecycle frames. При потере release карта отпускает lease в пределах оставшегося игрового времени; во время pause это не wall-clock SLA. Native/Star не перезапускаются.

Горячая кнопка в AMP требует настоящего Apply action и проверки интеграции Generic template; смена флага SupportsLiveSettingsChanges сама по себе этого не делает. В UI явно показывать, что применяется немедленно, что относится к следующим матчам и что требует restart. Перезапуск хоста посреди матча не использовать как незаметный способ выключения AI.

## 8. Этапы и проверяемые результаты

| Этап | Что сделать | Критерий завершения |
| --- | --- | --- |
| G0 — исследование | Pin исходников, варианты, схема, риски | Этот пакет документов; runtime ещё не подтверждён |
| G1 — bridge spike | Минимальная classic-карта, uplink/downlink/ACK без LLM | Два stock клиента 1.26a; сохранение требуемых слотов; нет desync; replay воспроизводит решения без сети |
| G2 — общий SDK | Фреймы, IDs/epochs, arbiter, snapshot/ACK и local fallback | Повтор, late/reordered/truncated packet, смена publisher и pause не меняют согласованность |
| G3 — service | Mock provider, scheduler, schema/semantic validator, budgets, independent recovery | Ошибка worker не перезапускает native/Star; bounded queue; off вызывает 0 API запросов |
| G4 — первый адаптер | Наблюдения, намерения, карта ownership/lifecycle | Afterlife shadow → active; смена сущности/раунда не принимает старую команду |
| G5 — provider и UI | OpenAI/compatible adapters, capability check, AMP/standalone | Свободный Model ID, одинаковый effective config, отсутствие ключа в logs/replay/status |
| G6 — второй адаптер | Подключить другую существующую карту | Только adapter/manifest и game-specific tests; service core без импортов Afterlife |
| G7 — нагрузка/выпуск | Параллельные scope, bounded soak, image/rollback | Нулевая межматчевая маршрутизация; отказ API не останавливает матч; проверенный immutable runtime |

В G1 также фиксируется происхождение common.j/blizzard.j/common.ai для точного клиента 1.26a. Наличие classic natives и успешный pjass не устанавливают версию prelude сами по себе.

G1 является go/no-go. Если канал, сохраняющий вместимость карты, не подтверждён, active mode остаётся недоступным. Уменьшение числа игроков, другой клиент или обязательный дополнительный game client должны стать отдельным явно выбранным вариантом, а не скрытым «исправлением».

## 9. Приёмка и доказательства

Минимум native acceptance: два клиента 1.26.0.6401; минимальная test-map и первый адаптер; штатный game chat; команда с ACK; 10 последовательных стартов; 60 минут игры; отсутствие desync; воспроизведение replay без API; restart раунда; уход/смена publisher; реальные paused/lagged игровые часы.

Отдельно: all-computer lobby не объявляется исполненным без реального клиента. Проверяется конфигурация максимальной вместимости карты и mixed humans/computers; перенос с fake-slot reference не должен резервировать роль молча.

Для каждого намерения: либо ACK с применением, либо код отказа. Каждая остановка бота более трёх секунд получает наблюдаемую причину (fight, cast, heal, blocked path, lifecycle, deliberate wait и т. п.); UNDEFINED разбирается. Это диагностика поведения, а не требование непрерывного движения при законной остановке.

Модель сравнивается с local baseline на одинаковых сценариях: выживание, достижение целей, полезные покупки/использование ресурсов, бессмысленная смена целей, latency и стоимость. LLM не считается улучшением только потому, что вернул валидный JSON.

Проверки нескольких матчей проводят после G1 и при готовой Map Cycle acceptance: минимум два одновременно исполняемых мира с разными adapters/epochs. Synthetic W3GS peers полезны для framing и routing, но не заменяют Warcraft в проверке JASS/desync.

Материалы проверки: source/map/client hashes, effective config без секретов, seed/scenario, compressed event journal, replay, ACK counts, fault timeline, usage ledger и список непроверенных пунктов. Большие сырые логи ограничены ротацией; в Git остаются компактные summaries/fixtures.

## 10. Порядок изменения репозиториев и выпуска

В публичном host repo: transport/service/operator integration, общие contracts, synthetic fixtures и открытые источники. В private maps repo: общий SDK, build injection и map-specific adapters/tests/docs. Частные карты, чужие бинарники, профили и credentials не копируются в public.

Разработка — отдельные ветки. Для каждой изменённой карты соблюсти собственные AGENTS: classic JASS, map-owned материалы, версия выпуска при игровых изменениях, build_all + полный test_all, MPQ readback и оригиналы. Документы исследования сами по себе не меняют версию карт и не выдают игровой acceptance.

Native patch digest, source revision, map SHA и protocol revision связываются в release identity. Runtime image публикуется по immutable digest в GHCR после проверки и аудита состава. Проверенный прежний image остаётся доступен для rollback; одно отключение AI должно возвращать локальное поведение без изменения карты и без удаления игровых данных.

Следующая конкретная задача: **G1 — минимальный двусторонний bridge spike на vanilla 1.26a с сохранением вместимости**, с mock-решением и map ACK, без расходования токенов модели.
