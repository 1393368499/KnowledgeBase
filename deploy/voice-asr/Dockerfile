# 基础镜像按 digest 固定，避免同名 tag 被上游重推。
# 注意：voice-asr 仍为 Python 3.13 —— onnxruntime==1.31.1 尚无 cp315 轮子（2026-11-13 构建实测失败）；
# 待其发布 cp315 轮子后随其余镜像一起切到 3.15。见 docs/python315-and-layout-migration-2026-11-13.md。
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ENV PYTHONUNBUFFERED=1 HF_HOME=/models
COPY deploy/voice-asr/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock && pip check
WORKDIR /srv
COPY deploy/voice-asr/server.py /srv/server.py
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
