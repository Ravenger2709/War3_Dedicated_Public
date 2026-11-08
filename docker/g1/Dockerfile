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

FROM packages AS toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git g++ make python3 libboost-filesystem-dev \
    libboost-system-dev libbz2-dev libgmp-dev zlib1g-dev cmake bison flex \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /repo
ENV PYTHONDONTWRITEBYTECODE=1
