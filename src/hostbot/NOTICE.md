# Native host dependencies and notices

GHost++ is fetched at build time from [ghostBot/ghostplusplus](https://github.com/ghostBot/ghostplusplus/tree/6ba90f426e96f80fe013a468d1e07151a510adbe), revision `6ba90f426e96f80fe013a468d1e07151a510adbe`. Trevor Hogan's GHost++ is Apache-2.0; the integration patch is maintained in this repository. The upstream source checkout and its bundled historical StormLib are not included in the runtime image.

GHost++ uses BNCSutil by Eric Naeseth (LGPL-2.1-or-later) and its upstream SQLite amalgamation (public domain). Builds use dynamic linking for BNCSutil and retain upstream notices. Source for the pinned components and the exact changes can be obtained using `scripts/build-ghost.sh` and `third_party/ghost.lock.json`; upstream LGPL obligations continue to apply.

MPQ handling uses pinned [StormLib 9.24](https://github.com/ladislav-zezula/StormLib/tree/3846f0b8e2c47320c6b499492496f3e3f2e76821) by Ladislav Zezula and contributors, under MIT. It uses the upstream fixes for failed file opens. LibTomCrypt and LibTomMath sources bundled with StormLib are public domain; system zlib and bzip2 retain their package notices. The old GHost++ bundled StormLib, including the historical Linux port, is **not compiled or linked**. Package copyright files are installed under `/opt/war3/share/licenses/`.

Other dynamic dependencies include Boost.Filesystem/System, GMP, zlib and bzip2; retain their package copyright files and comply with their own licenses. Star, Warcraft III, maps and account files are user-provided runtime inputs, never build inputs or image contents.
