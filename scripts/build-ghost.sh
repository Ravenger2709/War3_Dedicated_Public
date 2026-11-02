#!/usr/bin/env bash
# Native build. Optional dependencies are extracted locally, never installed.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
REV=6ba90f426e96f80fe013a468d1e07151a510adbe
JOBS=${JOBS:-4}
DEPS_ROOT=${GHOST_DEPS_ROOT:-}
SANITIZERS=${GHOST_SANITIZERS:-}
case "$SANITIZERS" in
    ''|address|undefined|address,undefined) ;;
    *) echo 'GHOST_SANITIZERS must be empty, address, undefined or address,undefined' >&2; exit 2 ;;
esac
OPT='-O2'
SANFLAGS=
if [[ -n "$SANITIZERS" ]]; then
    OPT='-O1 -g -fno-omit-frame-pointer'
    SANFLAGS="-fsanitize=$SANITIZERS"
fi
if [[ ${1:-} == --download-deps ]]; then
    mkdir -p build/deb-cache build/deps
    python3 - "$ROOT" <<'PY'
import hashlib,json,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]); cache=root/'build/deb-cache'
lock=json.loads((root/'third_party/ghost.lock.json').read_text())
for pkg in lock['ubuntu_noble_amd64_packages']:
    path=cache/pkg['filename']
    if not path.exists():
        subprocess.run(['apt-get','download',pkg['package']+'='+pkg['version']],cwd=cache,check=True)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=pkg['sha256']:
        raise SystemExit('Dependency SHA256 mismatch: '+str(path))
    subprocess.run(['dpkg-deb','-x',str(path),str(root/'build/deps')],check=True)
PY
    DEPS_ROOT="$ROOT/build/deps"
elif [[ $# -gt 0 ]]; then
    echo 'Usage: scripts/build-ghost.sh [--download-deps]' >&2; exit 2
elif [[ -d build/deps/usr/include/boost ]]; then
    DEPS_ROOT="$ROOT/build/deps"
fi
mkdir -p vendor build/ghost/lib build/ghost/share/licenses
if [[ ! -d vendor/ghostplusplus/.git ]]; then
    git clone --no-checkout https://github.com/ghostBot/ghostplusplus.git vendor/ghostplusplus
    git -C vendor/ghostplusplus checkout --detach "$REV"
fi
[[ $(git -C vendor/ghostplusplus rev-parse HEAD) == "$REV" ]] || {
    echo "Refusing to build unpinned checkout; expected $REV" >&2; exit 1;
}
PATCH="$ROOT/patches/ghostplusplus-modern-linux.patch"
if git -C vendor/ghostplusplus apply --check "$PATCH" 2>/dev/null; then
    git -C vendor/ghostplusplus apply "$PATCH"
elif ! git -C vendor/ghostplusplus apply --reverse --check "$PATCH" 2>/dev/null; then
    echo 'Checkout does not match the expected compatibility patch' >&2; exit 1
fi
# Exact patch comparison includes this newly added, declared source header.
git -C vendor/ghostplusplus add -N -- ghost/native_sha256.h
python3 scripts/ghost-check-source.py vendor/ghostplusplus
STORM_REV=3846f0b8e2c47320c6b499492496f3e3f2e76821
if [[ ! -d vendor/stormlib/.git ]]; then
    git clone --no-checkout https://github.com/ladislav-zezula/StormLib.git vendor/stormlib
    git -C vendor/stormlib checkout --detach "$STORM_REV"
fi
[[ $(git -C vendor/stormlib rev-parse HEAD) == "$STORM_REV" ]] || { echo 'Unpinned StormLib checkout' >&2; exit 1; }
[[ -z $(git -C vendor/stormlib status --porcelain --untracked-files=all) ]] || { echo 'Modified StormLib checkout refused' >&2; exit 1; }
cmake -S vendor/stormlib -B build/stormlib -DBUILD_SHARED_LIBS=ON -DSTORM_BUILD_TESTS=OFF \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DCMAKE_C_FLAGS="$OPT $SANFLAGS" -DCMAKE_CXX_FLAGS="$OPT $SANFLAGS" \
    -DCMAKE_SHARED_LINKER_FLAGS="$SANFLAGS"
cmake --build build/stormlib --parallel "$JOBS"
INC="-I$ROOT/vendor/stormlib/src" LIB="-L$ROOT/build/stormlib"
if [[ -n "$DEPS_ROOT" ]]; then
    DEPS_ROOT=$(cd "$DEPS_ROOT" && pwd)
    INC="$INC -I$DEPS_ROOT/usr/include -I$DEPS_ROOT/usr/include/x86_64-linux-gnu"
    LIB="$LIB -L$DEPS_ROOT/usr/lib -L$DEPS_ROOT/usr/lib/x86_64-linux-gnu"
fi
# Upstream makefiles do not track changes to command-line compiler flags.
# Clean our own vendor products when the profile changes, including returning
# from a sanitizer build to release. Never reuse partially instrumented objects.
GHOST_PATCH_SHA=$(sha256sum "$PATCH" | cut -d ' ' -f 1)
PROFILE="$OPT|$SANFLAGS|$INC|$LIB|$GHOST_PATCH_SHA"
if [[ ! -f build/ghost-build-profile ]] || [[ $(cat build/ghost-build-profile) != "$PROFILE" ]]; then
    for directory in bncsutil/src/bncsutil ghost; do
        make -C "vendor/ghostplusplus/$directory" clean
    done
    printf '%s\n' "$PROFILE" > build/ghost-build-profile
fi
make -C vendor/ghostplusplus/bncsutil/src/bncsutil -j"$JOBS" \
    CXXFLAGS="-std=gnu++11 $OPT $SANFLAGS -I../ -fPIC $INC" \
    CCFLAGS="$OPT $SANFLAGS -I../ -fPIC $INC" LDFLAGS="-shared $SANFLAGS $LIB -lgmp"
# SQLite is bundled upstream; MySQL is intentionally disabled. Modern Boost
# libraries have no -mt suffix. Thread/date_time are unused without MySQL.
make -C vendor/ghostplusplus/ghost -j"$JOBS" 'C++=g++ -std=gnu++11' \
    DFLAGS= OFLAGS="$OPT $SANFLAGS $INC" \
    LFLAGS="$SANFLAGS -L../bncsutil/src/bncsutil  $LIB -lbncsutil -lpthread -ldl -lz -lstorm -lboost_filesystem -lboost_system -lrt"
publish_elf() {
    local source=$1 destination=$2 temporary
    temporary=$(mktemp "${destination}.XXXXXXXX")
    install -m755 "$source" "$temporary"
    mv -f -- "$temporary" "$destination"
}
publish_elf vendor/ghostplusplus/ghost/ghost++ build/ghost/ghost++
publish_elf vendor/ghostplusplus/bncsutil/src/bncsutil/libbncsutil.so build/ghost/lib/libbncsutil.so
publish_elf build/stormlib/libstorm.so.9.22.0 build/ghost/lib/libstorm.so.9
ln -sf libstorm.so.9 build/ghost/lib/libStorm.so
g++ -std=gnu++11 $OPT $SANFLAGS  $INC src/hostbot/mpq-map-info.cpp \
    $LIB -lstorm -Wl,-rpath,'$ORIGIN/lib' -o build/ghost/mpq-map-info
install -m644 vendor/ghostplusplus/language.cfg build/ghost/share/language.cfg
install -m644 vendor/ghostplusplus/license.txt build/ghost/share/licenses/ghostplusplus-Apache-2.0.txt
install -m644 vendor/stormlib/LICENSE build/ghost/share/licenses/StormLib-MIT.txt
install -m644 src/hostbot/NOTICE.md build/ghost/share/licenses/NOTICE.md
head -n 28 vendor/ghostplusplus/bncsutil/src/bncsutil/nls.c > build/ghost/share/licenses/bncsutil-source-notice.txt
for license in LGPL-2.1 GPL-2 GPL-3; do
    [[ ! -f "/usr/share/common-licenses/$license" ]] || cp "/usr/share/common-licenses/$license" build/ghost/share/licenses/
done
for prefix in "${DEPS_ROOT:-/nonexistent}/usr/share/doc" /usr/share/doc; do
    for package in libboost1.83-dev libboost-filesystem1.83.0 libboost-system1.83.0 libgmp10 libbz2-1.0 zlib1g; do
        [[ ! -f "$prefix/$package/copyright" ]] || cp -L "$prefix/$package/copyright" "build/ghost/share/licenses/$package.copyright"
    done
done
if [[ -n "$DEPS_ROOT" ]]; then
    for stem in libboost_filesystem.so libboost_system.so libgmp.so libbz2.so; do
        for path in "$DEPS_ROOT/usr/lib/x86_64-linux-gnu/$stem".*; do
            [[ ! -f "$path" ]] || publish_elf "$path" "build/ghost/lib/$(basename "$path")"
        done
    done
fi
sha256sum build/ghost/ghost++ > build/ghost/binary.sha256
printf '%s\n' "$REV" > build/ghost/upstream-revision
printf '%s\n' "$PROFILE" > build/ghost/build-profile
echo "Native GHost++ built: $ROOT/build/ghost/ghost++"
