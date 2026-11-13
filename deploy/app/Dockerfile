# Default build is self-contained; BASE_IMAGE may select a prebuilt worker base.
# No source, secrets or business data from the host is copied implicitly.
ARG BASE_IMAGE=source-base
ARG OFFICE_JARS=empty-office-jars
FROM scratch AS empty-office-jars
FROM ${OFFICE_JARS} AS office-jars
# 基础镜像按 digest 固定（2026-11-11 从本机拉取的 python:3.15-slim manifest 解析所得）：
# 只写 tag 时上游重推同名 tag 会让"复审过的源码"与"实际构建的基座"脱钩。
# 升级基座时同步更新此 digest，并在构建记录里留痕（deploy/build-app.* 会记录产物 digest）。
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS source-base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 kb \
 && mkdir -p /var/lib/kb \
 && chown -R kb:kb /app /var/lib/kb
ENV KB_SKIP_DOTENV=1 KB_CELERY_QUEUE=kb_worker_v2

FROM ${BASE_IMAGE} AS font-runtime
WORKDIR /app
USER root

# OCR 自检和 PDF 转换共用字体。Debian 软件包走签名校验的官方 HTTPS 源。
ARG DEBIAN_URI=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_URI=https://security.debian.org/debian-security
RUN printf '%s\n' \
      'Types: deb' \
      "URIs: ${DEBIAN_URI}" \
      'Suites: trixie trixie-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
      '' \
      'Types: deb' \
      "URIs: ${DEBIAN_SECURITY_URI}" \
      'Suites: trixie-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
      > /etc/apt/sources.list.d/debian.sources

RUN set -eux; \
    apt-get \
      -o Acquire::Retries=1 \
      -o Acquire::http::Timeout=20 \
      -o Acquire::https::Timeout=20 \
      update; \
    apt-get \
      -o Acquire::Retries=1 \
      -o Acquire::http::Timeout=20 \
      -o Acquire::https::Timeout=20 \
      install -y --no-install-recommends \
        fonts-noto-cjk=1:20240730+repack1-1 \
        fonts-dejavu-core=2.37-8 \
        fontconfig=2.15.0-2.3; \
    rm -rf /var/lib/apt/lists/*

# Patch base packages even when the cached font layer is reused.
# 注意：apt/zlib1g **故意不钉版本**——这两项要的是"拿到稳定源里最新的安全修复"，
# 钉死版本反而会挡住后续安全更新。可复现性由基础镜像 digest + 显式记录产物 digest 提供；
# 依赖包（字体/office 运行库）则钉到具体版本，避免同一份源码构建出不同内容。
RUN set -eux; \
    printf 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.pgp] %s trixie-security main\n' \
      "${DEBIAN_SECURITY_URI}" > /tmp/kb-security.list; \
    apt-get -o Dir::Etc::sourcelist=/tmp/kb-security.list -o Dir::Etc::sourceparts=- \
      -o Acquire::Retries=1 -o Acquire::https::Timeout=20 update; \
    apt-get -o Dir::Etc::sourcelist=/tmp/kb-security.list -o Dir::Etc::sourceparts=- \
      -o Acquire::Retries=1 -o Acquire::https::Timeout=20 \
      install -y --only-upgrade apt zlib1g; \
    rm /tmp/kb-security.list; \
    rm -rf /var/lib/apt/lists/*

FROM font-runtime AS office-build
WORKDIR /build
RUN set -eux; \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update; \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
      'openjdk-21-jdk-headless=21.0.13+9-1~deb13u2'
ARG MAVEN_VERSION=3.9.11
ARG MAVEN_SHA512=03e2d65d4483a3396980629f260e25cac0d8b6f7f2791e4dc20bc83f9514db8d0f05b0479e699a5f34679250c49c8e52e961262ded468a20de0be254d8207076
ARG MAVEN_REPOSITORY=https://repo.maven.apache.org/maven2
COPY deploy/office/pom.xml deploy/office/maven-settings.xml deploy/office/jars.sha256 .
RUN --mount=from=office-jars,target=/verified-jars \
    set -eu; \
    mkdir -p /opt/office/lib; \
    if [ -n "$(ls -A /verified-jars)" ]; then \
      cp /verified-jars/*.jar /opt/office/lib/; \
    else \
      python -c 'import hashlib,io,os,urllib.request,zipfile; version=os.environ["MAVEN_VERSION"]; base=os.environ["MAVEN_REPOSITORY"]; url=f"{base}/org/apache/maven/apache-maven/{version}/apache-maven-{version}-bin.zip"; data=urllib.request.urlopen(url, timeout=40).read(); assert hashlib.sha512(data).hexdigest() == os.environ["MAVEN_SHA512"]; zipfile.ZipFile(io.BytesIO(data)).extractall("/opt")'; \
      chmod 0755 "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn"; \
      "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" -B -C -s maven-settings.xml \
        -Daether.transport.http.connectTimeout=15000 -Daether.transport.http.requestTimeout=30000 \
        org.apache.maven.plugins:maven-dependency-plugin:3.9.0:copy-dependencies -DoutputDirectory=/opt/office/lib; \
    fi; \
    cd /opt/office/lib; \
    test "$(find . -maxdepth 1 -type f | wc -l)" = "$(wc -l < /build/jars.sha256)"; \
    tr -d '\r' < /build/jars.sha256 | sha256sum --strict --check --status
COPY deploy/office/Convert.java .
RUN javac --release 21 -encoding UTF-8 -cp '/opt/office/lib/*' -d /opt/office Convert.java \
 && jlink --add-modules java.desktop,java.logging,java.naming,java.management,java.net.http,java.sql,jdk.charsets,jdk.unsupported,jdk.zipfs,jdk.xml.dom \
      --no-header-files --no-man-pages --compress=zip-6 --output /opt/office/java

FROM font-runtime AS office
RUN set -eux; \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update; \
    apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
      libharfbuzz0b=10.2.0-1+deb13u1 \
      libjpeg62-turbo=1:2.1.5-4 \
      liblcms2-2=2.16-2+deb13u2; \
    rm -rf /var/lib/apt/lists/*
COPY --from=office-build /opt/office /opt/office
COPY deploy/office/server.py /opt/office/server.py
COPY apps/knowledge/office_wire.py /opt/office/apps/knowledge/office_wire.py
RUN install -d -o kb -g kb -m 0750 /run/kb-office \
 && install -o kb -g kb -m 0600 /dev/null /run/kb-office/.volume \
 && /opt/office/java/bin/java -version \
 && python -m pip uninstall --yes pip \
 && python -c 'import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)'
USER kb
CMD ["python", "/opt/office/server.py"]
# 该镜像只提供转换套接字：不继承 worker 基座的 celery 探针，健康检查由 Compose 单独定义。
HEALTHCHECK NONE

FROM font-runtime AS runtime
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-deps -r requirements.txt \
 && python -m pip check \
 && python -m pip uninstall --yes pip \
 && python -c 'import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)'

RUN fc-list :lang=zh | grep -qi noto \
 && python -c "import subprocess;from PIL import Image,ImageDraw,ImageFont;p=subprocess.check_output(['fc-match','-f','%{file}','Noto Sans CJK SC']).decode().strip();assert p, 'fc-match 没找到中文字体';assert ('noto' in p.lower() or 'cjk' in p.lower()), ('字体不是 CJK：%s' % p);f=ImageFont.truetype(p,48);im=Image.new('L',(420,80),0);ImageDraw.Draw(im).text((5,5),'质保期 12 个月',fill=255,font=f);ink=sum(1 for v in im.getdata() if v>0);assert ink>800, ('中文渲染墨迹过少：%d' % ink);print('CJK font ok:', p, 'ink', ink)"

# ---- 用**当前源码**覆盖历史层（交付必须包含本机最新代码）----
COPY knowledgebase/ ./knowledgebase/
COPY apps/ ./apps/
COPY governance/ ./governance/
COPY manage.py ./

# 静态文件收集目录（STATIC_ROOT=<项目根>/data/staticfiles），交给运行用户
RUN mkdir -p /app/data/staticfiles /app/data/media/templates /usr/local/share/fonts/kb && chown -R kb:kb /app

USER kb

WORKDIR /app

EXPOSE 8000
CMD ["sh", "-c", "python manage.py collectstatic --noinput && exec gunicorn knowledgebase.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 300 --access-logfile - --error-logfile -"]
