"""F-07：设备协议版本显式校验（2026-11-11 复审）。

固件上传录音时带 ``protocol``（当前 1）。服务端此前只声明不校验：旧/新固件
混用时行为不可预期。现在显式拒绝不支持的版本，而不是静默按 1 处理。
"""

from __future__ import annotations

import hashlib
import io
import uuid
import wave

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def _device(user):
    api = APIClient()
    api.force_authenticate(user)
    created = api.post(
        "/api/v1/voice/devices/",
        {"name": "协议设备", "conversation_mode": "fixed"},
        format="json",
    )
    assert created.status_code == 201, created.data
    data = created.data
    device_api = APIClient()
    device_api.credentials(HTTP_AUTHORIZATION="Bearer " + data["device_token"])
    return data, device_api


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 8000)
    return out.getvalue()


def _upload(api, device, protocol):
    raw = _wav()
    payload = {
        "file": SimpleUploadedFile("audio.wav", raw, "audio/wav"),
        "turn_id": str(uuid.uuid4()),
        "config_version": device["config_version"],
        "audio_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if protocol is not None:
        payload["protocol"] = protocol
    return api.post("/api/v1/voice/device/turns/", payload, format="multipart")


def test_unsupported_protocol_is_rejected(user):
    device, device_api = _device(user)
    response = _upload(device_api, device, 99)
    assert response.status_code == 426, response.data
    body = response.data
    assert body["code"] == "protocol_unsupported"
    assert body["supported_protocols"] == [1]


def test_supported_protocol_is_accepted(user):
    device, device_api = _device(user)
    response = _upload(device_api, device, 1)
    assert response.status_code in (200, 202), response.data


def test_missing_protocol_keeps_backward_compatibility(user):
    """旧固件不带 protocol：保持可用（否则升级后端会打断所有老设备）。"""
    device, device_api = _device(user)
    response = _upload(device_api, device, None)
    assert response.status_code in (200, 202), response.data