# 基础镜像按 digest 固定，避免同名 tag 被上游重推。
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# 隧道二进制在构建期按 manifest.json 记录的 SHA-256 校验（来源见 deploy/voice-tunnel/README 或 artifacts/voice-tools/manifest.json）。
ARG CLOUDFLARED_SHA256=ae5e8add7884b0531d66bbca4570d1de491ff6da51e4ebe7eb160cdf241d9e42
COPY artifacts/voice-tools/cloudflared-linux-amd64 /usr/local/bin/cloudflared
RUN echo "${CLOUDFLARED_SHA256}  /usr/local/bin/cloudflared" | sha256sum -c - \
 && chmod 755 /usr/local/bin/cloudflared
COPY deploy/voice-tunnel/entrypoint.sh deploy/voice-tunnel/save_endpoint.py /srv/
RUN sed -i 's/\r$//' /srv/entrypoint.sh && chmod 755 /srv/entrypoint.sh
USER 10001:10001
ENTRYPOINT ["/srv/entrypoint.sh"]
