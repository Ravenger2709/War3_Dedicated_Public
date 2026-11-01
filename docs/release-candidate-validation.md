# Historical candidate validation: 2026-11-01

This report describes the pre-migration candidate and its historical dependency. The StormLib migration is validated separately in the release notes.

This documents local test evidence, **not a production acceptance gate or a public binary release**. Source tested: `d0294064016673aa4128ad382064e61981b4fe8b`; candidate Docker image ID `sha256:de03c04084a98671add6108ed7866382930a2b919a3f10dc8406c2b69d8174d7`. Native patch SHA-256: `d10bfedd130463bc143ce55154c154730244784371b5f0f5c21e1b0a30b1a5cd`.

- Candidate assembled from pinned upstream and checked against the production source context. Historical image-layer path audit: 21 layers, PASS. That scan does not prove the absence of every possible secret.
- Python offline suite in candidate, non-root and `--network none`: 240 tests passed, 4 skipped; separate relay suite: 152 tests passed. Star worker fixtures are synthetic.
- Map preset suite with an operator-supplied Footman map whose full SHA-256 matches the curated capability: 30 tests passed with candidate StormLib and no network. Different map hashes require a separate review and capability.
- Native GHost++ patched source, rebuilt with AddressSanitizer and UndefinedBehaviorSanitizer in a network-isolated builder: 73 gameplay tests and 7 publication tests passed. The gameplay suite includes a 66-second solo AI match and rehosting. Sanitizer text log SHA-256 before temporary cleanup: `61ad5b57bb2a1863488d45d822620f5294a12a62be3f7c6f0c406901d3778d88`.

- Actual candidate with official Star 4.3.75 and operator-provided Warcraft metadata and map, `--network none`, synthetic profile: 900-second soak PASS (elapsed 1015.844 seconds). Cold startup, persistent-prefix restart, native SIGKILL recovery, SQLite persistence, graceful shutdown, no remaining vendor processes, no zombies and no game-engine execution were observed. No OOM. Probe result SHA-256: `1c0e8a248c608a539e1777f32ca43ce27a568588e5de2700e1f82f58a1d70182`.
- Separate actual Star SIGKILL recovery probe: PASS, elapsed 129.839 seconds, no OOM. Probe SHA-256: `17f87043b896f52bdd521f33566ad78b9cdfe067d0d481e2027e97e23f5e608a`.
- Production supervisor offline cohort: PASS, elapsed 138.354 seconds. Star, relay and native SIGKILL recovery, duplicate-profile ownership, operator stop/start, SQLite persistence, no false online readiness and no leftover processes were checked. Probe SHA-256: `9d57d6a38252a20ceb3a18e58f2d563fc2fb48b72bcfc21f94fb2faf9e9587d7`.

Actual iCCup authentication and external client tests were not performed for this candidate. The checks above do not establish a complete source-matched acceptance gate or third-party distribution rights. In particular, the GHost++ snapshot includes `StormPortLinux.cpp`, which identifies a separate 2001 copyright holder and states all rights reserved without a distribution grant in that file. The later official StormLib MIT license by itself does not establish permission to redistribute that historical Linux port. Resolve its applicable terms or replace it with a verified licensed implementation before publishing a binary image. Do not copy an old gate to this image.
