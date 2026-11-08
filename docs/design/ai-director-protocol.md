# AI Director: проект протокола и transport gate

Статус: **DRAFT / NOT IMPLEMENTED / Warcraft III 1.26a NOT VERIFIED**. Дата: 2026-11-08.

Это семантический контракт для разработки и план эксперимента. Opcode references подтверждены открытыми исходниками; конкретные wire bytes, ограничения клиента и игровое поведение должны быть закреплены fixtures после G1. Этот документ не является готовой инструкцией для включения active AI.

[Общий план](ai-director-plan.md) · [Исследование](ai-director-research.md) · [Host/AMP](ai-director-host-amp-plan.md)

> Обновление G1 от 2026-11-08: в feature-ветке реализован и проверен одним
> реальным клиентом [контрольный двусторонний мост](ai-director-g1.md).
> Он занимает один человеческий слот (11 реальных игроков); это не завершает
> требование сохранения всех 12 мест. Остальная архитектура ниже остаётся
> проектом: LLM/worker/AMP-интеграция не реализованы и не включены.

## 1. Почему bridge нужен с обеих сторон

GHost++ пересылает команды, но не вычисляет игровой мир. JASS 1.26a не получает универсального HTTP-клиента только от размещения карты на GHost. Сервис не может восстановить достоверные HP, инвентарь и карту из lobby telemetry.

Подтверждённые транспортные опоры:

- [GHost W3MMD parser](https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/statsw3mmd.cpp) разбирает SyncStoredInteger action 0x6B с gamecache strings/integer.
- [W3HMC SendString/ProcessAction](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/ghostw3hmc.cpp) использует gamecache uplink и помещает trigger-chat action 0x60 в m_Actions.
- [GHost action serializer](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/gameprotocol.cpp) включает PID, длину и payload в incoming-actions; CRC формируется для outgoing subpacket.
- [Queue/broadcast/replay](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/game_base.cpp) отправляет очередь всем клиентам и сохраняет actions в replay.

W3MMD — основание для map→host, а W3HMC — кандидат двустороннего подхода. Обычный SendAllChat/W3GS_CHAT_FROM_HOST не приравнивается к action 0x60 и не считается синхронным RPC.

## 2. Критический выбор sender PID и вместимость

Legacy W3HMC создаёт одного fake human на матч. CreateFakePlayer(SID,name) требует существующий OPEN slot и переводит его в OCCUPIED/computer=0. Дополнительного игрового слота это не создаёт; текущий virtual-host PID удаляется при старте и не доказывает работоспособный sender во время игры.

Для карт с полностью распределёнными слотами такой перенос меняет вместимость и lifecycle. Основной путь G1 должен сохранять исходные роли карты.

**Кандидат A: использовать PID существующего реального участника для host-origin reserved trigger-chat action.** По исходникам serializer не имеет отдельного запрета для human PID и отправляет action всем клиентам. Это обоснование эксперимента, а не доказательство исполнения в 1.26a.

Проверить отдельно: обработку собственного action на машине участника, соответствие trigger identity, checksum, повторное исполнение, отсутствие desync, смену sender и replay. Если хотя бы один пункт не подтверждён, active transport остаётся недоступным.

Кандидат B — dedicated fake slot — допустим только как явно выбранный профиль карты с документированным изменением вместимости. Он не заменяет автоматически критерий сохранения игроков. Observer, Player(12+) и временный virtual host не объявляются «свободным слотом» без испытания.

В любом варианте требуется реальный клиент, исполняющий симуляцию. Отсутствие людей в lobby вместе с fake sender не создаёт headless game engine.

## 3. Transport gate G1 без LLM

Создать маленькую карту plain JASS2/W3I v25 для 1.26.0.6401. Она содержит один тестовый actor, monotonic tick, выделенный callback и ограниченные snapshot/ACK. Native host работает с mock director, который возвращает заранее заданный move/hold intent.

Последовательность:

1. Два реальных stock клиента запускают одинаковый MPQ.
2. Все клиенты создают bridge handles в одинаковом порядке; сверяются map build/protocol и callback identity. Action 0x60 имеет два wire identity поля: GetHandleId(trigger) и одинаковый порядок allocations сами по себе не доказывают их mapping. G1 фиксирует capture/calibration либо отдельно доказанное преобразование и проверяет перестановку initializer.
3. Единый избранный publisher отправляет HELLO и фиксированный snapshot.
4. Native привязывает payload к фактическому CBaseGame, проверяет карту и выдаёт SESSION_ACCEPT.
5. Host injects одну 0x60-команду через штатную action queue.
6. Все клиенты одинаково проверяют и применяют её; карта возвращает applied ACK. Digest/счётчик исполнения от обоих реальных клиентов обязателен в evidence; один ACK publisher не доказывает одинаковый результат.
7. Повтор команды не повторяет эффект. Истёкшая/ошибочная команда имеет rejected ACK.
8. Replay, открытый без AI service и сети, даёт ту же последовательность и результат.

Дополнительные прогоны: разные слоты publisher, mixed human/computer, полный slot budget, отправка настоящего игрового chat, pause/unpause, искусственная задержка provider, disconnect publisher, новый round, иной map SHA, два одновременно активных матча.

Фиксировать exact client build, native/map SHA, packet fixture, callback mapping, ACK/digest timeline и replay. Native fake peers и pjass не доказывают клиентское исполнение.

## 4. Где перехватывать actions

Uplink parser отделить от статистического m_Stats: AI не требует map_type=w3mmd и не отключает существующую Dota/W3MMD статистику.

Если выбран borrowed human PID, зарезервированное ingress пространство должно принадлежать host bridge. Проверять client-origin reserved trigger actions от всех peer, включая elected publisher, **до добавления в m_Actions**. В legacy CGame::EventPlayerAction дополнительный W3HMC hook расположен после базового enqueue; для фильтрации это слишком поздно.

Не искать байт 0x60/0x6B как подстроку: такие байты допустимы внутри других структур и строк. Нужен проверенный versioned action decoder с корректными длинами, лимитами и правилами для всех встречаемых action kinds. Повреждённый или непонятный пакет не становится основанием для частичной переинтерпретации. Политику dropped/rejected packet закрепить тестами, чтобы исправные игровые приказы не исчезали.

Ingress принимает только нужный cache namespace/protocol и выбранного publisher. Native полностью владеет полями match scope; значения карты не позволяют выбрать другой CBaseGame. Quotas ограничивают нагрузку от любого клиента. Uplink при этом остаётся недоверенным наблюдением от peer: выбранный клиент способен подделать 0x6B, а host без движка не проверит истинность HP/ресурсов. Map validation ограничивает допустимость эффекта, но не доказывает истинность данных для стратегии. Сопоставление digest нескольких клиентов полезно для диагностики, но не превращает протокол в полноценный anti-cheat.

W3HMC не переносится целиком: в reference есть global m_Game/m_GCFilename, слабые assumptions об открытом слоте и HTTP requests из game actions. Новая реализация должна иметь per-match state, bounded IPC и фиксированный provider endpoint из operator config.

## 5. Детерминизм

Все игровые мутации и все handle allocations выполняются одинаково у всех клиентов. GetLocalPlayer допускается только для выбора отправляющей копии network telemetry при подтверждённой корректности этого примитива; внутри локальной ветки нельзя создавать/удалять юниты, менять ресурсы, AI-состояние, sequence counters или игровые handle allocations.

Клиенты формируют одну и ту же telemetry-последовательность из синхронного состояния. Избранный real publisher отправляет её в host; остальные могут отправлять bounded checksum для диагностики. Local private memory не становится источником shared decision.

Приказ принимается по общему ingress action и применяется в фиксированной фазе map tick. Во время pause старение по simulation tick не подменяется wall clock. HTTP deadline использует monotonic wall time сервиса; map TTL — simulation tick.

При потере publisher повторное избрание происходит из синхронного списка участников. Bridge epoch увеличивается согласованно через protocol handshake; старые решения аннулируются. До нового ACK управление локальное. После crash worker native generation и текущая map epoch сверяются заново; неизвестные прежние планы не восстанавливаются как активные.

Replay содержит принятые downlink actions и достаточную последовательность handshake/epochs, а не обращения к API. Никакие ответы из текущего интернета во время replay не запрашиваются.

## 6. Scope и идентификаторы

Host envelope дополняется trusted bridge, не генерируется моделью:

| Поле | Источник / смысл |
| --- | --- |
| protocol_version | Совместимость service/native/SDK |
| instance_id | Уникальный runtime instance |
| session_generation | Существующее поколение supervisor/native |
| host_counter + match_id | Native match identity |
| map_sha256 | Sealed hosted map bytes из manifest |
| adapter_id + adapter_version | Разрешённый capability contract |
| control_scope_id | Trusted команда/аудитория наблюдения, allowed actors и правила видимости |
| session_nonce | Компактная связь map session и host scope; не секрет |
| bridge_epoch | Новое соединение/выбор publisher/control epoch |
| round_epoch | Внутренний рестарт мира при прежнем матче |
| config_revision | Версия effective AI policy |
| snapshot_seq + snapshot_tick | Основание решения |
| decision_seq + command_seq | Дедупликация принятого плана/команд |
| expires_at_tick | Map TTL |
| actor_id + entity_generation | Логическая сущность и её текущая инкарнация |
| target_id + target_generation | Отдельная версия каждой изменяемой цели; zone ID может быть immutable в map revision |

Ключи scheduler, assembly, provider context, sequence counters, receipt cache и lease включают полный match scope + bridge_epoch + round_epoch + control_scope_id. Scope задаётся native/adapter manifest и связывается с allowed actors/observation audience; модель не выбирает его и не переводит actor в другую команду.

Model output содержит только scoped intentions из allowlist; envelope заполняется и проверяется сервисом. Native сверяет полный scope, карта — компактные session/epochs и actor generation. Человеческие имена, название lobby и preset ID не заменяют match identity.

JASS не использует pointer/handle ID юнита как постоянный identity. Actor registry выдаёт стабильный integer и повышает generation при удалении/замене объекта. Смена оружия, revive, morph и restart могут сделать прежний target недействительным. Поля integer находятся в допустимом диапазоне signed 32-bit; wrap требует нового epoch, не молчаливого сравнения через переполнение.

## 7. Сообщения и семантика

Минимальный набор: HELLO, SESSION_ACCEPT, SNAPSHOT, EVENT, DECISION, COMMAND_ACK, HEARTBEAT, RELEASE_CONTROL, SESSION_END.

Предлагаемый semantic DECISION после trusted envelope, до кодирования в JASS framing:

~~~json
{
  "protocol_version": 1,
  "session_nonce": 731,
  "bridge_epoch": 2,
  "round_epoch": 5,
  "control_scope_id": 1,
  "config_revision": 4,
  "snapshot_seq": 88,
  "snapshot_tick": 2400,
  "decision_seq": 12,
  "expires_at_tick": 2460,
  "intents": [
    {
      "command_seq": 19,
      "actor_id": 3,
      "entity_generation": 7,
      "kind": "retreat_to_zone",
      "target_zone_id": 6,
      "priority": 40
    }
  ]
}
~~~

Все числа примера условны. В этом профиле tick=0.25s, lease=60 ticks=15s от snapshot_tick, не 15s после получения; другая карта объявляет свою единицу времени. Acceptance требует возраст snapshot не больше 6s, а продолжение уже принятого плана дополнительно ограничено expires_at_tick. Семантический JSON не посылается длинной строкой через игровой chat.

ACK различает received, accepted, started, completed, rejected и expired. Для неподдерживаемого эффекта используется capability_absent; для исчезнувшей цели — stale_entity; для запрещённого actor — not_ai_owned; для перехвата экстренным AI — preempted. Успешный возврат JASS Issue…Order — не подтверждение, что покупка, лечение или движение достигли результата: карту просят подтвердить эффект по наблюдаемому состоянию.

Exactly-once и гарантированная доставка не обещаются. Используется bounded retry; доставка может закончиться timeout, а обработка допускает дубликаты и идемпотентна по command_seq в полном scope. Предлагается receipt window последних 128 команд + monotonic watermark: дубликат внутри окна возвращает сохранённый ACK, старый seq вне окна получает receipt_unavailable/stale_duplicate и никогда не исполняется заново. Новая команда на прежний snapshot должна пройти проверку возраста.

## 8. Framing и ограничения

Game wire — компактные числа, enum IDs и short strings. JSON используется между Python/native и для диагностики. Предлагаемые стартовые лимиты ниже должны уточняться по G1; они не являются заявленными пределами движка:

- application payload ≤96 bytes на wire frame;
- ≤64 fragments, ≤6144 bytes на логическое сообщение;
- не больше одного собираемого snapshot и decision на scope;
- фрагменты имеют message type, session/epoch, seq, index/count, length и checksum;
- incomplete assembly теряется целиком по TTL, не применяется частично;
- host resource budget до 16 telemetry fragments/монотонную wall секунду и 4 downlink fragments/монотонную wall секунду на scope, ограниченная burst-очередь;
- очередь IPC ≤32 KiB на message; bounded total, latest-snapshot replacement и fair scheduling между матчами.

64 fragments/6144 bytes — только абсолютный hard ceiling, например для handshake/diagnostic payload. Для active profile предлагаются гораздо меньшие operational caps: SNAPSHOT ≤8 fragments/768 bytes, DECISION ≤2 fragments/192 bytes. При указанном rate это примерно 0.5s uplink + до 4s HTTP + 0.5s downlink + 0.5s queue/tick margin = 5.5s при проверенном соотношении игровых и wall часов 1:1. Это расчёт бюджета, не измеренная гарантия.

Admission проверяет весь путь относительно оставшихся 6 игровых секунд свежести и 15 секунд lease от snapshot. Конверсия wall/simulation clock, game speed и margin проверяются в G1; окончательная актуальность всегда определяется map tick. Effective HTTP deadline ограничен минимумом настройки и остатка end-to-end бюджета. При нехватке времени или encoded size: сократить наблюдение/число независимых намерений до dispatch либо оставить local AI. Частично собранный/семантически урезанный DECISION не исполняется. После ответа admission повторяется до enqueue; большие framing limits не обходят freshness gate.

Checksum обнаруживает случайное повреждение, но не является аутентификацией. Секретный API key в карте отсутствует. Reserved downlink spoofing от клиентов блокируется до enqueue. Uplink source/namespace проверяется и ограничивается, однако не аутентифицирует истинность JASS telemetry; повторные семантические проверки в карте ограничивают возможный эффект. Nonce и trigger ID не считаются секретами.

Service не принимает HTTP URL, файловый путь, сырой JASS или argv из карты/model. Обычный player chat не входит в prompt по умолчанию; разрешённый текст считается данными, а команды остаются enum/schema-controlled.

## 9. Validate → enqueue → revalidate → acknowledge

До dispatch: model capability, schema, scope, budget и deadline.

До injection: актуальные native match/map/epoch, выбранный sender, разрешённый adapter, byte/rate limit, отсутствие завершения матча.

В map: lifecycle ready, COMPUTER+PLAYING ownership, живой actor и generation, отдельный target generation для изменяемой цели, свежий snapshot/TTL, capability, bounds, видимость, ресурсы, inventory space/cooldown по данным адаптера и arbiter priority.

Перед самим эффектом: повторная проверка изменяемых условий. Если предмет исчез или золота уже не хватает, команда завершается отказом. Никаких CreateItem/бесплатных ресурсов/телепорта для маскировки неудавшегося обычного действия.

При отключении module native перестаёт принимать новые DECISION, service отменяет jobs. Priority RELEASE_CONTROL и lifecycle frames остаются разрешены и освобождают внешний lease; если пакет не дошёл, finite map lease всё равно возвращает локальный AI в пределах игрового времени. Во время pause wall-clock срок возврата не обещается. Внутри native это отдельная ветка, без падения realm health.

## 10. Критерии решения после spike

| Исход G1 | Следующее действие |
| --- | --- |
| 1.26a, исходные slots, ACK, desync/replay проверки прошли | Зафиксировать decoder/fixtures и перейти к SDK/service |
| Работает только с reserved slot | Явно описать изменённый профиль; default active остаётся заблокированным |
| Работает только с DLL/другим клиентом | Это отдельный транспорт/профиль, не результат для vanilla 1.26a |
| Только synthetic packets или успешная компиляция | G1 остаётся pending |
| Native канал не подтверждён | Сохранить local AI и результаты исследования; не строить ложную «готовность API» |

Результат G1 должен быть маленьким воспроизводимым commit: fixture map, native patch, smoke procedure и compact evidence. Большая интеграция AMP/provider следует после этого доказательства.
