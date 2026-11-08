# Готовые Warcraft III AI решения: исследование перед внедрением

Проверено 2026-11-08. Статус: **research / no external code vendored**.

Цель поиска: сервис моделей рядом с текущим GHost++ для multiplayer custom maps на обычном Warcraft III 1.26a. Совпадение слова Warcraft в названии, запуск на другом патче или наличие MCP не подтверждают пригодность для этого deployment.

## Вывод

Готовые API/LLM проекты найдены. OpenWar3AI ближе всего к внешнему программному управлению юнитами; wc3env полезен как открытая среда с разделением macro/micro; Tavern демонстрирует LLM+map-daemon подход. Их runtime assumptions отличаются от текущего headless GHost и classic 1.26a.

Для текущего проекта наиболее близкая транспортная опора — W3HMC/th-ghost. Она позволяет изучить map↔host обмен без установки DLL игрокам. **Готовой проверенной интеграции 1.26a с сохранением всех слотов и текущей Map Cycle архитектуры в исследовании не найдено.** Поэтому выбран небольшой новый transport/service, использующий проверенные идеи, с отдельным native go/no-go этапом.

## Проверенные проекты

Ссылки на commits фиксируют исследованные версии. Дата — дата HEAD commit, а не дата обновления страницы/последнего push.

| Проект | Что есть в коде/README | Предел применимости | Что использовать |
| --- | --- | --- | --- |
| [OpenWar3AI](https://github.com/OPENXXAI/OpenWar3AI/tree/ffd7dd25a3c2478f457b74bde89d23e605b24243), 2026-09-28 | Python SDK, snapshots/actions, WebSocket gateway, MCP, Farsight, RPG companion | Windows, **1.27a / Game.dll 1.27.0.52240**, injected runtime. Ключевая DLL без исходников; это работающий game client, не host relay | API/capability/receipt patterns; не объявлять готовым портом 1.26a |
| [wc3env / wc3agent](https://github.com/offpolicyinc/wc3env/tree/78ed5cc0d18cd4961d7453a343215e689b2e24a3), 2026-10-20 | Реальная среда Warcraft, state/actions/stepping, медленный LLM planner и быстрый controller | **1.29.2.9232**, injected DLL, Windows/Wine; [compatibility](https://github.com/offpolicyinc/wc3env/blob/78ed5cc0d18cd4961d7453a343215e689b2e24a3/docs/compatibility.md) отдельно ограничивает LAN/network/custom maps | Планирование, отсечение stale responses, разделение ownership; transport не подходит напрямую |
| [warcraftsim](https://github.com/RobinKa/warcraftsim/tree/da3efe38fb2479d834db383a73bc2e0ad858f525), 2026-11-06 | Python API, Wine game runtime, JASS harness, native shim, RL/selfplay | Legacy TFT **1.29.2**, w3shim.dll и реальный игровой процесс; не GHost multiplayer module | Возможная последующая исследовательская лаборатория, не runtime первого выпуска |
| [Tavern](https://github.com/satchmakua/Tavern/tree/0e9585d7c8041c4a7f6ad5003e2780a45bba2eb4), 2026-07-23 | asyncio daemon, Ollama, chat/voice/personas, directive protocol | **Reforged**, TypeScript→Lua, FileIO/Preload, BlzSendSyncData. [DIR handler](https://github.com/satchmakua/Tavern/blob/0e9585d7c8041c4a7f6ad5003e2780a45bba2eb4/map-mod/src/bridge/directives.ts) пока логирует; AMAI routing — TODO | Узкие directives и daemon boundaries; не называть законченной LLM боёвкой |
| [W3HMC / th-ghost](https://github.com/cipherxof/th-ghost/tree/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a), 2018-07-17 | Gamecache actions в host, trigger-chat actions обратно в map | Modified GHost, изменённый map script, известные receiver identities; **один fake-human slot**. В default config версия29; native доказательства 1.26a не найдены | Лучший узкий transport reference; отдельный PoC на целевом патче |
| [AMAI](https://github.com/SMUnlimited/AMAI/tree/74f317f62a5e7c06282221cf0a6812f63cda58a6), 2026-10-25 | Развитый rule-based melee AI, стратегии и micro | TFT profile указывает optimal **1.24–1.28**, supported **1.24+**, но это melee, а не универсальный custom-map adapter | Идеи тактики; текущие AI карты уже имеют собственных исполнителей |
| [W3Droid](https://github.com/W3Droid/w3droid/tree/801ddb95f555ed0579579f8d7c8b8c3f6e0d03c6), 2022-09-12, [pyw3droid](https://github.com/tanghyd/pyw3droid) | Protocol definitions, protobuf/WebSocket и Python wrapper | Upstream сообщает об отсутствии release; usable native backend и 1.26a не подтверждены | Schema ideas; не готовый backend |
| [w3gjs](https://github.com/PBug90/w3gjs/tree/8ccf5463098f1dc88b6938f6fd7483d36352b117), 2026-10-29 | Replay/action parser, в т. ч. 0x60/0x6B | Не AI и не runtime, parser coverage не доказывает correctness нового transport | Сопоставление packet/replay fixtures после проверки лицензии конкретного кода |

Дополнительный близкий пример: [DeepSeek + MemoryHack demo](https://xgm.guru/p/wc3/deepseek). Автор описывает старый Warcraft 1.26–1.27, Python и файловый обмен; указаны MemoryHack и один игрок. Это исследовательский пример, не plain-JASS multiplayer решение. В текущие ограничения проекта такой runtime не входит.

## Лицензии и границы переноса

Это перечень условий из просмотренных source/license файлов для планирования состава зависимостей; юридическая оценка конкретного выпуска здесь не выполнялась.

| Компонент | Обнаруженная лицензия / ограничение | Решение |
| --- | --- | --- |
| OpenWar3AI открытый SDK | Apache-2.0 | Можно рассматривать отдельные открытые элементы с attribution |
| OpenWar3AI prebuilt runtime | [NOTICE](https://github.com/OPENXXAI/OpenWar3AI/blob/ffd7dd25a3c2478f457b74bde89d23e605b24243/NOTICE): бинарники исключены из Apache, source отсутствует | Не планировать самостоятельный исходный порт DLL |
| wc3env, warcraftsim, Tavern, W3Droid | Основной код MIT, зависимости могут иметь другие условия | Зафиксировать pin, notices и конкретный файл перед копированием |
| W3HMC C++ | GPL-3.0-or-later в headers / LICENSE | Новый native код либо собственная реализация протокола, либо перенос с сохранением условий/атрибуции; не перелицензировать автоматически в Apache |
| W3HMC JASS gist | Авторство есть; отдельная явная лицензия в просмотренной копии не найдена | Не копировать текст автоматически по лицензии C++ |
| AMAI | [Собственная лицензия](https://github.com/SMUnlimited/AMAI/blob/74f317f62a5e7c06282221cf0a6812f63cda58a6/LICENSE), включая условия использования в mod projects | Не включать как MIT/GPL dependency по ошибке |
| w3gjs и MemoryHack demo | Лицензия используемого фрагмента отдельно не зафиксирована | Только исследование до проверки условий |

В этой ветке нет скопированного third-party implementation, бинарников или новых зависимостей. Публичные документы не публикуют частные map source/assets.

## W3HMC: подтверждённые механизмы и ограничения

### Uplink и downlink

[ghostw3hmc.cpp](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/ghostw3hmc.cpp):

- ProcessAction ищет SyncStoredInteger opcode 0x6B и разбирает gamecache filename/mission/key/integer.
- SendString создаёт 0x60 trigger-chat payload с receiver identity и помещает CIncomingAction(m_PID, ...) в m_Actions.
- Это два разных механизма; обычный CHAT_FROM_HOST не является заменой downlink.
- SetMapData хранит m_Game/m_GCFilename в общем объекте. Такой lifetime не подходит для нескольких независимых одновременно идущих матчей.

[game_base.cpp](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/game_base.cpp): CreateFakePlayer требует существующий открытый слот. GetNewPID не создаёт новый JASS Player. Старый код может вернуть PID даже если слот не удалось занять; прямое копирование недопустимо.

### Сохранение вместимости

Для карт без свободного слота предложен эксперимент: injection reserved 0x60 под PID уже присутствующего реального человека. [Serializer](https://github.com/cipherxof/th-ghost/blob/63df93c6b52b9d9420dcf77481bb016e0a0f4c6a/ghost/gameprotocol.cpp) не разделяет fake/human sender и формирует общий CRC, а queue сохраняется в replay. **Это только подтверждение точки расширения C++, не доказательство клиентской семантики 1.26a.**

Для безопасной реализации нужен разбор client action boundaries и блокировка client-origin reserved callback до enqueue. Byte substring filter непригоден. После ухода sender — синхронное переизбрание с epoch; пока оно не подтверждено, местный AI продолжает работу.

### Receiver identity

[Историческая JASS копия](https://gist.github.com/Valkrysa/2d103cb910d2cac655e995013b6c969f) зависит от очень раннего создания trigger/chat-event, выбранного Player и JassHelper initialization order. Автор предлагает capture/calibration при иной последовательности.

Нельзя утверждать, что GetHandleId(trigger) автоматически даёт оба wire fields. Начальный PoC должен захватить реальное событие тестовой карты, связать receiver descriptor с точным map SHA и проверить одинаковую инициализацию на двух клиентах. После map rebuild identity может поменяться.

### Что не переносить

Не включать старый server HTTP engine в native loop, global singleton матча, URL из карты, небounded HTTP results, thread model и byte scanner. Цель — малый per-match protocol bridge и новый асинхронный service с уже существующими lifecycle/release contracts хост-бота.

## Матрица достоверности

| Утверждение | Статус на дату исследования |
| --- | --- |
| Есть внешние Warcraft III API и LLM experiments | Подтверждено указанными исходниками |
| GHost gamecache parser даёт отправную точку для map→host | Подтверждено кодом |
| W3HMC реализует host→map через 0x60 | Подтверждено кодом |
| W3HMC гарантированно работает именно в stock 1.26a | Не подтверждено |
| Borrowed real PID сохраняет все игровые места без desync | Гипотеза G1 |
| Один GetHandleId полностью решает receiver descriptor | Не подтверждено |
| GHost/LLM могут самостоятельно симулировать мир без Warcraft client | Нет: host не исполняет движок |
| Любую неизменённую custom map достаточно выбрать в AMP | Нет: нужен map SDK/adapter |
| Tavern уже передаёт LLM directives в боевой AMAI | Не подтверждается просмотренным handler: TODO |
| OpenWar3AI runtime можно самостоятельно портировать по открытым source | Source DLL отсутствует |

Авторская публикация W3HMC: [TriggerHappy — Web Requests from your map](https://www.hiveworkshop.com/threads/web-requests-http-from-your-map-and-more.307208/). Она полезна как описание подхода, но не заменяет native 1.26a test.

Выбранный порядок работ и критерии G1 закреплены в [основном плане](ai-director-plan.md) и [протоколе](ai-director-protocol.md).
