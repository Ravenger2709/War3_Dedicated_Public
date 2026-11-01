FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
ARG UBUNTU_SNAPSHOT=20261028T000000Z
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ADD --checksum=sha256:8a0a754aaafed72cc8efb8c1731808133284a84fbfc76a4fcc73fce9e01b4187 https://snapshot.ubuntu.com/ubuntu/20261028T000000Z/pool/main/c/ca-certificates/ca-certificates_20260816ubuntu0.24.04.1_all.deb /tmp/ca.deb
RUN dpkg-deb -x /tmp/ca.deb / && mkdir -p /etc/ssl/certs \
    && cat /usr/share/ca-certificates/mozilla/*.crt > /etc/ssl/certs/ca-certificates.crt \
    && rm /tmp/ca.deb \
    && printf 'Acquire::Check-Valid-Until "false";\nAPT::Update::Error-Mode "any";\nAcquire::Retries "3";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/50snapshot \
    && printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-security\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\nSnapshot: no\n' "$UBUNTU_SNAPSHOT" > /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
       python3 libasan8 libubsan1 libboost-filesystem1.83.0 libboost-system1.83.0 libgmp10 libbz2-1.0 zlib1g \
    && dpkg-query -W > /qa-packages.txt && rm -rf /var/lib/apt/lists/*
USER 10001:10001
WORKDIR /results
ENTRYPOINT ["python3", "/project/tests/reliability/ghost_shutdown.py"]
