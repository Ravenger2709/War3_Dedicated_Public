#!/bin/bash
# Sourced by the pinned /ampstart.sh after creating amp, before gosu amp:amp.
# Isolate variables and shell options from the upstream entrypoint.
(
    set -eu
    socket=/var/run/docker.sock
    if [[ $(id -u) != 0 || ! -S "$socket" ]]; then
        echo '[Error] AMP control requires root startup and a Docker socket at /var/run/docker.sock' >&2
        exit 1
    fi
    id amp >/dev/null
    socket_gid=$(stat -Lc '%g' "$socket")
    if group_entry=$(getent group "$socket_gid"); then
        socket_group=${group_entry%%:*}
    else
        socket_group="war3-docker-${socket_gid}"
        groupadd --gid "$socket_gid" "$socket_group"
    fi
    # Append, preserving the primary AMP group and upstream supplemental groups.
    usermod --append --groups "$socket_group" amp
    # Check the same shimmed privilege drop the upstream handoff uses; never chmod/chown
    # the host socket or assume that the host's group is named 'docker'.
    if ! gosu amp:amp test -r "$socket" || ! gosu amp:amp test -w "$socket"; then
        echo '[Error] AMP user cannot access the mounted Docker socket' >&2
        exit 1
    fi
    echo "[Info] AMP control Docker socket group GID ${socket_gid} ready"
) || exit "$?"
