# Build context: this directory only. Both upstream images are immutable.
# The amd64 AMP config ID is a231eb7883435d1cada5ac6bacc96a8def201d8ed46f0be89769a05b9371036c.
FROM docker.io/library/docker:29.5.1-cli@sha256:93afea9ac780f8706540408cba1e801ba285071476b0c2bff424a0ca0037869c AS cli

FROM docker.io/cubecoders/ampbase:debian@sha256:e77e043faed5c7fdf9b8e6de01318caa0ceb4ff8443a1e116ce0587b081ce266
USER root
# Copy only the static client. No daemon, entrypoint, plugins or TLS setup.
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
COPY gosu /usr/local/sbin/gosu
COPY hook.sh /ampstart.d/50-war3-docker-socket.sh
RUN chmod 0755 /usr/local/sbin/gosu \
    && chmod 0644 /ampstart.d/50-war3-docker-socket.sh \
    && echo 'f835491486b6d31c9445cd38bfcc4ce9d0b0258334f61ebd221abbe69613a985  /ampstart.sh' | sha256sum --check - \
    && python3 --version \
    && docker --version \
    && command -v gosu getent groupadd usermod stat \
    && ! command -v dockerd
LABEL org.opencontainers.image.title="War3 AMP control" \
      org.opencontainers.image.description="Trusted AMP controller using the host Docker socket for sibling runtimes" \
      org.opencontainers.image.source="https://github.com/Ravenger2709/War3_Dedicated_Public" \
      war3.amp-control.base-config-id="sha256:a231eb7883435d1cada5ac6bacc96a8def201d8ed46f0be89769a05b9371036c" \
      war3.amp-control.docker-cli-version="29.5.1"
# Inherit tini and ampstart.sh unchanged. The narrow gosu shim retains groups.
