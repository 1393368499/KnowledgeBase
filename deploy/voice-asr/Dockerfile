FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 HF_HOME=/models
COPY deploy/voice-asr/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock && pip check
WORKDIR /srv
COPY deploy/voice-asr/server.py /srv/server.py
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
