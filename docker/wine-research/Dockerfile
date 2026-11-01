ARG UBUNTU_IMAGE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ${UBUNTU_IMAGE} AS base
ARG UBUNTU_SNAPSHOT=20261028T000000Z
ENV DEBIAN_FRONTEND=noninteractive
ADD --checksum=sha256:8a0a754aaafed72cc8efb8c1731808133284a84fbfc76a4fcc73fce9e01b4187 https://snapshot.ubuntu.com/ubuntu/20261028T000000Z/pool/main/c/ca-certificates/ca-certificates_20260816ubuntu0.24.04.1_all.deb /tmp/ca.deb
RUN dpkg-deb -x /tmp/ca.deb / && mkdir -p /etc/ssl/certs \
    && cat /usr/share/ca-certificates/mozilla/*.crt > /etc/ssl/certs/ca-certificates.crt \
    && rm /tmp/ca.deb \
    && printf 'Acquire::Check-Valid-Until "false";\nAPT::Update::Error-Mode "any";\n' > /etc/apt/apt.conf.d/50snapshot \
    && printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-security\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\nSnapshot: no\n' "$UBUNTU_SNAPSHOT" > /etc/apt/sources.list.d/ubuntu.sources

FROM base AS diagnostics
RUN apt-get update && apt-get install -y --no-install-recommends gcc-mingw-w64-i686-posix gpg && rm -rf /var/lib/apt/lists/*
COPY load-only.c /src/load-only.c
RUN i686-w64-mingw32-gcc -Os -static -s -Wall -Wextra -o /load-only.exe /src/load-only.c

ADD --checksum=sha256:d965d646defe94b3dfba6d5b4406900ac6c81065428bf9d9303ad7a72ee8d1b8 https://dl.winehq.org/wine-builds/winehq.key /winehq.asc
RUN gpg --batch --dearmor -o /winehq.gpg /winehq.asc

FROM base AS runtime
ARG WINE_VERSION=11.0.0.0~noble-1
COPY --from=diagnostics /winehq.gpg /etc/apt/keyrings/winehq.gpg
RUN --mount=type=cache,id=war3-wine-apt,target=/var/cache/apt,sharing=locked \
    dpkg --add-architecture i386 \
    && printf 'Types: deb\nURIs: https://dl.winehq.org/wine-builds/ubuntu\nSuites: noble\nComponents: main\nArchitectures: amd64 i386\nSigned-By: /etc/apt/keyrings/winehq.gpg\n' > /etc/apt/sources.list.d/winehq.sources \
    && printf 'Acquire::Retries "3";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80research-retries \
    && apt-get update && apt-get install -y --no-install-recommends \
       winehq-stable=${WINE_VERSION} wine-stable=${WINE_VERSION} \
       wine-stable-amd64=${WINE_VERSION} wine-stable-i386:i386=${WINE_VERSION} \
       xvfb xauth x11-utils python3 procps strace iproute2 fonts-dejavu-core ca-certificates \
    && mkdir -p /opt/wine-research /work /evidence \
    && dpkg-query -W > /opt/wine-research/packages.txt \
    && wine --version > /opt/wine-research/version.txt \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 research && useradd -u 10001 -g 10001 -M -d /work research \
    && chown -R 10001:10001 /work /evidence
COPY --from=diagnostics /load-only.exe /opt/wine-research/load-only.exe
COPY observe.py /opt/wine-research/observe.py
ENV HOME=/work WINEPREFIX=/work/prefix WINEARCH=win32 \
    WINEDLLOVERRIDES=winemenubuilder.exe=d DISPLAY=:99 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "/opt/wine-research/observe.py"]
