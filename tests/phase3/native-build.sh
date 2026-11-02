#!/usr/bin/env bash
# Execute only inside the pinned local --network=none builder; staged sources only.
set -euo pipefail
cd /repo
export GHOST_DEPS_ROOT=/deps JOBS=2
bash scripts/build-ghost.sh
python3 tests/offline/test_ghost_source_guard.py
flags=(-std=gnu++11 -O1 -g -fno-omit-frame-pointer -Ivendor/ghostplusplus/ghost -I/deps/usr/include -I/deps/usr/include/x86_64-linux-gnu -Ivendor/ghostplusplus/bncsutil/src -Ivendor/stormlib/src)
if [[ -n ${GHOST_SANITIZERS:-} ]]; then flags+=("-fsanitize=$GHOST_SANITIZERS"); fi
g++ "${flags[@]}" -Dmain=ghost_original_main -c vendor/ghostplusplus/ghost/ghost.cpp -o build/native-test-ghost.o
objects=()
for object in vendor/ghostplusplus/ghost/*.o; do
    [[ $object == */ghost.o ]] || objects+=("$object")
done
gcc -shared -fPIC -O2 tests/phase3/native-fence-crash.c -ldl -o build/native-fence-crash.so
for driver in phase3/native-driver bridge/native-publication-driver; do
    output=build/$(basename "$driver")
    g++ "${flags[@]}" "tests/$driver.cpp" build/native-test-ghost.o "${objects[@]}" \
        -Lbuild/ghost/lib -L/deps/usr/lib/x86_64-linux-gnu -lbncsutil -lStorm -lboost_filesystem -lboost_system -lgmp -lbz2 -lz -lpthread -ldl -lrt -o "$output"
done
export LD_LIBRARY_PATH=/repo/build/ghost/lib:/deps/usr/lib/x86_64-linux-gnu
export WAR3_STORM_LIBRARY=/repo/build/ghost/lib/libStorm.so
export PYTHONDONTWRITEBYTECODE=1 ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
PYTHONPATH=/repo python3 tests/bridge/test_publication_file_race.py
python3 tests/phase3/native-test.py
python3 tests/phase3/native-vote-reminder-test.py
python3 tests/phase3/native-multimatch-test.py
python3 tests/phase3/native-next-preset-test.py
python3 tests/phase3/native-map-bounds-test.py
python3 tests/phase3/native-sealed-map-test.py
python3 tests/phase3/native-lifecycle-test.py
python3 tests/phase3/native-reservation-gate-test.py
python3 tests/phase3/native-fenced-close-test.py
python3 tests/phase4/native-sha256-test.py
python3 tests/phase4/native-kick-vote-test.py
python3 tests/phase4/native-manifest-envelope-test.py
python3 tests/phase4/native-startup-catalog-test.py
python3 tests/phase4/native-preload-ipc-test.py
python3 tests/phase4/native-status-cycle-test.py
python3 tests/phase4/native-startup-ipc-test.py
python3 tests/phase4/native-runtime-backend-e2e-test.py
PYTHONPATH=/repo python3 tests/phase4/test_native_match_telemetry.py
python3 tests/phase4/native-controller-cycle-e2e-test.py
python3 tests/phase4/native-controller-countdown-e2e-test.py
python3 tests/phase4/native-controller-capacity3-test.py
python3 tests/phase4/native-controlled-restart-test.py
python3 tests/phase4/native-catalog-close-recovery-test.py
python3 tests/phase4/native-manual-preload-test.py
python3 tests/phase3/native-preset-fence-test.py
python3 tests/bridge/native-publication-test.py
