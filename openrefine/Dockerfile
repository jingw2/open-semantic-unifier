# OpenRefine has no official Docker Hub image. Build from the official release
# tarball and verify the SHA-256 published in the GitHub release.
FROM eclipse-temurin:21-jre@sha256:d77e090ae1f6970c047b443c6f0a6c1358b0426d44432bb5a7753408cd43de0d
ARG OPENREFINE_VERSION=3.10.1
ARG OPENREFINE_SHA256=806e38e415fa98c2dee400de575992514557a4e37e82c670b7cd2e34d667c4ba
RUN curl -fsSL -o /tmp/openrefine.tar.gz "https://github.com/OpenRefine/OpenRefine/releases/download/${OPENREFINE_VERSION}/openrefine-linux-${OPENREFINE_VERSION}.tar.gz" \
 && echo "${OPENREFINE_SHA256}  /tmp/openrefine.tar.gz" | sha256sum -c - \
 && mkdir -p /opt \
 && tar -xzf /tmp/openrefine.tar.gz -C /opt \
 && mv "/opt/openrefine-${OPENREFINE_VERSION}" /opt/openrefine \
 && rm /tmp/openrefine.tar.gz \
 && mkdir -p /data
WORKDIR /opt/openrefine
EXPOSE 3333
CMD ["./refine", "-i", "0.0.0.0", "-d", "/data", "-m", "1024M"]
