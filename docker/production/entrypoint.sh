#!/bin/sh
set -eu
umask 077
[ "$(id -u)" -ne 0 ] || { echo 'non-root runtime required' >&2; exit 64; }
mkdir -p /data/state/mapcfg /data/state/native /data/state/bridge \
    /data/session/home /data/session/launcher
for config in /data/config/maps/*.cfg; do
    [ -f "$config" ] || continue
    ln -sfn "$config" "/data/state/mapcfg/${config##*/}"
done
exec "$@"
