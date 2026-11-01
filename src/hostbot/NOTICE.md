# Исходники и уведомления зависимостей

Исходники GHost++ не включены в Git этого проекта. Сборка загружает
`https://github.com/ghostBot/ghostplusplus.git`, строго
`6ba90f426e96f80fe013a468d1e07151a510adbe`; локальные изменения перечислены
в `patches/ghostplusplus-modern-linux.patch`. Исходный checkout с применённым
патчем остаётся в игнорируемом `vendor/ghostplusplus/`.

| Компонент | Проверенное уведомление |
| --- | --- |
| GHost++ 17.2 | Copyright Trevor Hogan 2008, Apache License 2.0; `license.txt`, заголовки `ghost/*.cpp` |
| BNCSutil | Copyright Eric Naeseth 2004–2006; LGPL 2.1 или более поздняя; заголовки `bncsutil/src/bncsutil/{bncsutil.h,nls.c}`. Отдельного COPYING в данном snapshot нет. |
| SQLite 3.7.13 | Встроен в `ghost/sqlite3.c`; авторский отказ от copyright в начале amalgamation |
| Старый StormLib | Copyright Ladislav Zezula 1998–2009, Linux port Marko Friedemann 2001, Mac port Sam Wilkins. В этом snapshot нет отдельной лицензии основного StormLib; заголовки и уведомления сохранены. |
| StormLib PKLIB | В исходниках есть copyright Ladislav Zezula и строковое уведомление PKWARE Inc. 1989–1995. Не считаем весь этот каталог автоматически Apache/GPL/MIT. |
| zlib | Системная zlib, не bundled zlib 1.2.3. Уведомление пакета Ubuntu копируется при наличии. |
| bzip2 | Системная libbz2; bundled `StormLib/stormlib/bzip2/LICENSE` сохранена отдельно как исторический notice |
| Boost 1.83 | Системные/распакованные Boost.Filesystem и Boost.System; copyright-файлы пакетов Ubuntu сохраняются |
| GMP 6.3 | Системная/распакованная shared library; copyright-файл пакета Ubuntu и доступные стандартные тексты LGPL/GPL сохраняются |

Первичные ссылки на snapshot:

- https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/license.txt
- https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/bncsutil/src/bncsutil/nls.c
- https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/StormLib/stormlib/StormLib.h
- https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/StormLib/stormlib/pklib/explode.c
- https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/StormLib/stormlib/bzip2/LICENSE
- https://github.com/ghostBot/ghostplusplus/blob/6ba90f426e96f80fe013a468d1e07151a510adbe/ghost/sqlite3.c

Современный авторский StormLib публикует MIT:
https://github.com/ladislav-zezula/StormLib/blob/master/LICENSE.
Это справочная информация, а не доказательство лицензии каждого файла старого
snapshot. До публичной поставки образа нужно уточнить условия старого StormLib
и сохранить соответствующие исходники/уведомления динамических зависимостей.
Автоматической публикации образа наш build-скрипт не выполняет.
