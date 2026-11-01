# Контракт GHost++ для supervisor

Закреплён upstream `https://github.com/ghostBot/ghostplusplus.git`, ревизия
`6ba90f426e96f80fe013a468d1e07151a510adbe` (17.2, 2013-12-15).

Запуск: argv `["/absolute/project/scripts/ghost-run.sh", "/absolute/runtime/ghost.cfg"]`.
Нативный executable: `build/ghost/ghost++`; wrapper выставляет пути библиотек и
делает `exec`, stdout наследуется. Единственный аргумент executable — cfg.
GHost++ не читает команды из stdin. Рабочая директория должна быть приватной,
доступной для записи (SQLite, относительные пути); предпочтительно runtime.

Реальные настройки: `autohost_gamename`, `autohost_startplayers`,
`autohost_maxgames`, `autohost_owner`, `bot_maxgames`, `bot_defaultmap`.
Настроек `bot_autohostgame...` в выбранной ревизии нет.
Для одной партии одновременно: `bot_maxgames = 1`, `autohost_maxgames = 1`.
Autohost проверяется каждые 30 секунд, autostart каждые 10 секунд. До старта
нужны подтверждение карты и три pong. Локальный патч добавляет
`bot_countdownseconds = 5` (0..60, шаг таймера 500 мс); по умолчанию сохраняется
задержка upstream. Значения в countdown-чате — полусекундные шаги.
`prepare-map.py --max-players N` читает классический `war3map.w3i` через
собранный native helper и закрывает лишние человеческие слоты штатными
`map_slotN`, сохраняя команды, цвета, расы и компьютерные слоты карты.
Без этой опции максимум задаётся самой картой. `--players` — минимум старта.
`--no-autohost` устанавливает штатный `autohost_maxgames = 0`.

Точные строки stdout из исходников (без временного префикса; он есть в файле log):

```text
[GHOST] GHost++ Version 17.2 (without MySQL support)
[GHOST] offline initialization complete
[GHOST] offline initialization failed
[GHOST] creating game [NAME #1]
[GAME: NAME #1] listening on port 6113
[GAME: NAME #1] player [OfflineOne|127.0.0.1] joined the game
[GAME: NAME #1] started loading with 2 players
[GAME: NAME #1] player [OfflineOne] finished loading in 0.00 seconds
[GAME: NAME #1] finished loading with 2 players
[GAME: NAME #1] is over (no players left)
[GHOST] deleting game [NAME #1]
[GHOST] deleting current game [NAME #1]
[GAME: NAME #1] error listening on port 6113
[GHOST] stopped auto hosting, map config file [PATH] is invalid
[!!!] caught signal 15, exiting nicely
[GHOST] all threads finished, exiting nicely
```

`creating game` печатается до bind: LOBBY подтверждает только `listening`.
Локальный `offline initialization complete` означает READY и появляется
после загрузки валидной карты и проверки БД, только при отсутствии BNCS.
Это позволяет supervisor сохранять READY при отключённом autohost. При
невалидной карте или ошибке БД печатается только `offline initialization failed`.
`started loading` означает начало загрузки, `finished loading` — IN_GAME.
`deleting current game` — удаление лобби; `deleting game` — удаление матча.
Не считать каждую строку `is over` успешным игровым завершением: причины различны.

Патч добавляет обработку SIGTERM по штатному пути SIGINT. Первый сигнал ждёт
активные матчи; повторный переключает на принудительный выход. Операторский
timeout supervisor должен учитывать это. В LAN-конфигурации `bnet_server =`
пустой, без логинов. Объявления ограничены `udp_broadcasttarget = 127.0.0.1`.

Сборка и протокольный тест оформляются скриптами `build-ghost.sh`,
`prepare-map.py`, `test-ghost.sh`. Подробные фактические результаты:
`research/dedicated-build.md`.

Входы Docker builder: `scripts/build-ghost.sh`, `scripts/ghost-check-source.py`,
`third_party/ghost.lock.json`, `patches/ghostplusplus-modern-linux.patch`,
весь `src/hostbot/`. Установленные зависимости: g++, gcc, make, git, Python3,
Boost.Filesystem/System, GMP, bz2, zlib и их development headers. Никаких
Warcraft/iCCup файлов для сборки не нужно. `build/ghost/` содержит executable,
shared libraries, language.cfg, helper `mpq-map-info`, notices и ревизию.
