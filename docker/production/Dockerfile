# All three refs are local ID-derived tags checked by build-production-image.py.
# No package download, proprietary input or account profile enters this build.
ARG NATIVE_IMAGE
ARG WINE_IMAGE
ARG HELPER_IMAGE
FROM ${NATIVE_IMAGE} AS native
FROM ${HELPER_IMAGE} AS helper
USER root
COPY docker/wine-research/quit-launcher.c /build/quit-launcher.c
RUN i686-w64-mingw32-gcc -Os -static -s -Wall -Wextra -Werror \
    -o /build/quit-launcher.exe /build/quit-launcher.c

FROM ${WINE_IMAGE} AS production
USER root
COPY --from=native /opt/war3/ /opt/war3/
COPY --from=native /usr/lib/x86_64-linux-gnu/libboost_filesystem.so.1.83.0 /usr/lib/x86_64-linux-gnu/
COPY --from=native /usr/lib/x86_64-linux-gnu/libgmp.so.10.5.0 /usr/lib/x86_64-linux-gnu/
RUN test -e /usr/lib/x86_64-linux-gnu/libgmp.so.10 \
    || ln -s libgmp.so.10.5.0 /usr/lib/x86_64-linux-gnu/libgmp.so.10
# Remove stale Python copied from native, then install the entire integrated package.
RUN rm -rf /opt/war3/src /opt/war3/examples && mkdir -p /opt/war3/src
COPY src/ /opt/war3/src/
COPY --from=helper /build/quit-launcher.exe /opt/war3/bin/quit-launcher.exe
COPY docker/production/entrypoint.sh /opt/war3/entrypoint.sh
COPY docker/healthcheck.py /opt/war3/healthcheck.py
COPY docker/production/runtime-contract.json /opt/war3/share/runtime-contract.json
COPY config/star-vendor-files.json /opt/war3/config/star-vendor-files.json
COPY config/supervisor-iccup.example.json /opt/war3/examples/supervisor-iccup.json
COPY config/presets.example.json config/presets.schema.json /opt/war3/examples/
COPY production-context-manifest.json production-source-sha256.json /opt/war3/share/
RUN chmod 755 /opt/war3/entrypoint.sh \
    && mkdir -p /data/config /data/maps /data/warcraft /data/state /data/sqlite \
       /data/logs /data/replays /data/prefix /data/session /vendor/star \
       /vendor/profile /vendor/redist /vendor/game \
    && chown -R 10001:10001 /data \
    && LD_LIBRARY_PATH=/opt/war3/lib ldd /opt/war3/bin/ghost++ > /opt/war3/share/production-native-dependencies.txt \
    && ! grep -q 'not found' /opt/war3/share/production-native-dependencies.txt \
    && dpkg-query -W > /opt/war3/share/production-packages.txt
ENV PYTHONPATH=/opt/war3 LD_LIBRARY_PATH=/opt/war3/lib \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WINEPREFIX=/data/prefix \
    HOME=/data/session/home WINEARCH=win32 DISPLAY=:99 \
    WINEDLLOVERRIDES=winemenubuilder.exe=d
WORKDIR /data
USER 10001:10001
EXPOSE 6113/tcp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=3s --start-period=90s --retries=3 \
    CMD ["python3", "/opt/war3/healthcheck.py"]
ENTRYPOINT ["/opt/war3/entrypoint.sh"]
CMD ["python3", "-m", "src.supervisor", "--config", "/data/config/supervisor.json"]
