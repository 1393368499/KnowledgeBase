FROM python:3.13-slim
WORKDIR /srv
COPY deploy/voice-gateway/server.py /srv/server.py
USER 10001:10001
CMD ["python", "/srv/server.py"]
