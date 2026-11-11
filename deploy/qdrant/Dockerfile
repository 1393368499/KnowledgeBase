FROM qdrant/qdrant:v1.16.3@sha256:0425e3e03e7fd9b3dc95c4214546afe19de2eb2e28ca621441a56663ac6e1f46

USER root
RUN set -eux; \
    sed -i 's|^URIs: http://deb.debian.org/debian-security$|URIs: https://security.debian.org/debian-security|; s|^URIs: http://deb.debian.org/debian$|URIs: https://deb.debian.org/debian|' /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update; \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 upgrade -y; \
    rm -rf /var/lib/apt/lists/* /qdrant/static; \
    mkdir /qdrant/static
