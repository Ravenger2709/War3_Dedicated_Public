#!/usr/bin/env bash
# Run ONLY inside the local network=none build container (see test docstring).
set -euo pipefail
cd /repo
export GHOST_DEPS_ROOT=/deps JOBS=2
bash scripts/build-ghost.sh
python3 tests/offline/test_ghost_source_guard.py
flags=(-std=gnu++11 -O1 -g -fno-omit-frame-pointer -Ivendor/ghostplusplus/ghost -I/deps/usr/include -I/deps/usr/include/x86_64-linux-gnu -Ivendor/ghostplusplus/bncsutil/src -Ivendor/ghostplusplus/StormLib)
if [[ -n ${GHOST_SANITIZERS:-} ]]; then flags+=("-fsanitize=$GHOST_SANITIZERS"); fi
g++ "${flags[@]}" -Dmain=ghost_original_main -c vendor/ghostplusplus/ghost/ghost.cpp -o build/publication-driver-ghost.o
objects=()
for object in vendor/ghostplusplus/ghost/*.o; do
    [[ $object == */ghost.o ]] || objects+=("$object")
done
g++ "${flags[@]}" tests/bridge/native-publication-driver.cpp build/publication-driver-ghost.o "${objects[@]}" \
    -Lbuild/ghost/lib -L/deps/usr/lib/x86_64-linux-gnu -lbncsutil -lStorm -lboost_filesystem -lboost_system -lgmp -lbz2 -lz -lpthread -ldl -lrt -o build/native-publication-driver
export LD_LIBRARY_PATH=/repo/build/ghost/lib:/deps/usr/lib/x86_64-linux-gnu
export PYTHONDONTWRITEBYTECODE=1 ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
PYTHONPATH=/repo python3 tests/bridge/test_publication_file_race.py
python3 tests/bridge/native-publication-test.py
