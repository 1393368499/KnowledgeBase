# 基础镜像按 digest 固定，避免同名 tag 被上游重推。
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /srv
COPY deploy/voice-gateway/server.py /srv/server.py
USER 10001:10001
CMD ["python", "/srv/server.py"]
