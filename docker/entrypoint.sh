#!/bin/sh
set -eu
umask 077
# GHost extracts common.j/blizzard.j beside its map configuration. Keep the
# operator configuration read-only and place generated scripts in writable state.
mkdir -p /data/state/mapcfg
for config in /data/config/maps/*.cfg; do
    [ -f "$config" ] || continue
    ln -sfn "$config" "/data/state/mapcfg/${config##*/}"
done
exec "$@"
