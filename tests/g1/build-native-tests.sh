#!/usr/bin/env bash
# Local toolchain container, --network none. No engine, credentials or AMP.
set -euo pipefail
cd /repo
export PYTHONPATH=/repo JOBS=2
: "${WAR3_STORM_LIBRARY:?Supply the unsanitized MIT StormLib path for Python MPQ fixtures}"
[[ -f "$WAR3_STORM_LIBRARY" ]]
[[ $(stat -c %i /sys/fs/cgroup) == 1 ]] || { echo "Full read-only cgroup hierarchy required for concurrency tests" >&2; exit 2; }
bash scripts/build-ghost.sh
flags=(-std=gnu++11 -O1 -g -fno-omit-frame-pointer -Ivendor/ghostplusplus/ghost -Ivendor/ghostplusplus/bncsutil/src -Ivendor/stormlib/src)
if [[ -n ${GHOST_SANITIZERS:-} ]]; then flags+=("-fsanitize=$GHOST_SANITIZERS"); fi
g++ "${flags[@]}" -Dmain=ghost_original_main -c vendor/ghostplusplus/ghost/ghost.cpp -o build/native-test-ghost.o
objects=()
for object in vendor/ghostplusplus/ghost/*.o; do
 [[ $object == */ghost.o ]] || objects+=("$object")
done
g++ "${flags[@]}" tests/phase3/native-driver.cpp build/native-test-ghost.o "${objects[@]}" -Lbuild/ghost/lib -lbncsutil -lStorm -lboost_filesystem -lboost_system -lgmp -lbz2 -lz -lpthread -ldl -lrt -o build/native-driver
export LD_LIBRARY_PATH=/repo/build/ghost/lib
export ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
python3 tests/g1/native-bridge-test.py
python3 tests/phase3/native-vote-reminder-test.py
python3 tests/phase3/native-multimatch-test.py
