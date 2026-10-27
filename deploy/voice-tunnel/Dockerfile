FROM python:3.13-slim
COPY artifacts/voice-tools/cloudflared-linux-amd64 /usr/local/bin/cloudflared
RUN chmod 755 /usr/local/bin/cloudflared
USER 10001:10001
ENTRYPOINT ["cloudflared"]
