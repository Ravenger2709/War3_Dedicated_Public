#!/usr/bin/env bash
# Execute only inside the pinned local --network=none builder; staged sources only.
set -euo pipefail
cd /repo
export GHOST_DEPS_ROOT=/deps JOBS=2
bash scripts/build-ghost.sh
python3 tests/offline/test_ghost_source_guard.py
flags=(-std=gnu++11 -O1 -g -fno-omit-frame-pointer -Ivendor/ghostplusplus/ghost -I/deps/usr/include -I/deps/usr/include/x86_64-linux-gnu -Ivendor/ghostplusplus/bncsutil/src -Ivendor/ghostplusplus/StormLib)
if [[ -n ${GHOST_SANITIZERS:-} ]]; then flags+=("-fsanitize=$GHOST_SANITIZERS"); fi
g++ "${flags[@]}" -Dmain=ghost_original_main -c vendor/ghostplusplus/ghost/ghost.cpp -o build/native-test-ghost.o
objects=()
for object in vendor/ghostplusplus/ghost/*.o; do
    [[ $object == */ghost.o ]] || objects+=("$object")
done
for driver in phase3/native-driver bridge/native-publication-driver; do
    output=build/$(basename "$driver")
    g++ "${flags[@]}" "tests/$driver.cpp" build/native-test-ghost.o "${objects[@]}" \
        -Lbuild/ghost/lib -L/deps/usr/lib/x86_64-linux-gnu -lbncsutil -lStorm -lboost_filesystem -lboost_system -lgmp -lbz2 -lz -lpthread -ldl -lrt -o "$output"
done
export LD_LIBRARY_PATH=/repo/build/ghost/lib:/deps/usr/lib/x86_64-linux-gnu
export PYTHONDONTWRITEBYTECODE=1 ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
python3 tests/phase3/native-test.py
python3 tests/bridge/native-publication-test.py
