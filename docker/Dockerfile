ARG UBUNTU_IMAGE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ${UBUNTU_IMAGE} AS packages
ARG UBUNTU_SNAPSHOT=20261028T000000Z
ENV DEBIAN_FRONTEND=noninteractive
# Signed snapshot indexes fix all transitive package versions, not just build deps.
# Bootstrap TLS from a checksum-pinned Ubuntu package. The minimal base has no
# CA store; downloading with BuildKit avoids disabling TLS verification in APT.
ADD --checksum=sha256:8a0a754aaafed72cc8efb8c1731808133284a84fbfc76a4fcc73fce9e01b4187 https://snapshot.ubuntu.com/ubuntu/20261028T000000Z/pool/main/c/ca-certificates/ca-certificates_20260816ubuntu0.24.04.1_all.deb /tmp/ca-certificates.deb
RUN dpkg-deb -x /tmp/ca-certificates.deb / \
    && mkdir -p /etc/ssl/certs \
    && cat /usr/share/ca-certificates/mozilla/*.crt > /etc/ssl/certs/ca-certificates.crt \
    && rm /tmp/ca-certificates.deb \
    && printf 'Acquire::Check-Valid-Until "false";\nAPT::Update::Error-Mode "any";\n' > /etc/apt/apt.conf.d/50snapshot \
    && printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-security\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\nSnapshot: no\n' "$UBUNTU_SNAPSHOT" > /etc/apt/sources.list.d/ubuntu.sources

FROM packages AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git g++ make python3 libboost-filesystem-dev \
    libboost-system-dev libbz2-dev libgmp-dev zlib1g-dev cmake \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts/build-ghost.sh scripts/build-ghost.sh
COPY scripts/ghost-check-source.py scripts/ghost-check-source.py
COPY third_party/ghost.lock.json third_party/ghost.lock.json
COPY patches/ghostplusplus-modern-linux.patch patches/ghostplusplus-modern-linux.patch
COPY src/hostbot/ src/hostbot/
RUN JOBS=2 bash scripts/build-ghost.sh \
    && mkdir -p /install/bin /install/lib /install/share \
    && cp build/ghost/ghost++ /install/bin/ghost++ \
    && cp -a build/ghost/lib/. /install/lib/ \
    && cp -a build/ghost/share/. /install/share/ \
    && cp build/ghost/upstream-revision /install/share/ \
    && dpkg-query -W > /install/share/build-packages.txt \
    && rm -rf vendor build

FROM packages AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 libboost-filesystem1.83.0 libboost-system1.83.0 libgmp10 libbz2-1.0 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hostbot \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /data --shell /usr/sbin/nologin hostbot \
    && mkdir -p /data/config /data/maps /data/logs /data/replays /data/state /data/warcraft \
    && chown -R 10001:10001 /data
COPY --from=builder /install/ /opt/war3/
COPY src/supervisor/ /opt/war3/src/supervisor/
COPY src/connector/ /opt/war3/src/connector/
COPY docker/healthcheck.py /opt/war3/healthcheck.py
COPY docker/entrypoint.sh /opt/war3/entrypoint.sh
COPY docker/ghost.cfg /opt/war3/examples/ghost.cfg
COPY config/supervisor.example.json /opt/war3/examples/supervisor.json
COPY context-manifest.json /opt/war3/share/context-manifest.json
COPY context-sha256.json /opt/war3/share/context-sha256.json
RUN chmod 755 /opt/war3/entrypoint.sh \
    && dpkg-query -W > /opt/war3/share/runtime-packages.txt \
    && LD_LIBRARY_PATH=/opt/war3/lib ldd /opt/war3/bin/ghost++ \
    && ! LD_LIBRARY_PATH=/opt/war3/lib ldd /opt/war3/bin/ghost++ | grep 'not found'
ENV PYTHONPATH=/opt/war3 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    LD_LIBRARY_PATH=/opt/war3/lib HOME=/data
WORKDIR /data
USER 10001:10001
EXPOSE 6113/tcp 8080/tcp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python3", "/opt/war3/healthcheck.py"]
ENTRYPOINT ["/opt/war3/entrypoint.sh"]
CMD ["python3", "-m", "src.supervisor", "--config", "/data/config/supervisor.json"]
