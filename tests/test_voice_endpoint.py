import json
import importlib.util
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import pytest
from tests.api_client import APIClient
from tests.test_voice_devices import manager


@pytest.mark.django_db
def test_endpoint_requires_web_login_and_reports_saved_address(user, settings, tmp_path):
    path = tmp_path / "current.json"
    settings.KB_VOICE_ENDPOINT_FILE = path
    api = manager(user)
    assert APIClient().get("/api/v1/voice/endpoint/").status_code in (401, 403)
    assert api.get("/api/v1/voice/endpoint/").data["server"] == ""
    path.write_text(json.dumps({"server": "https://example-test.trycloudflare.com", "updated_at": "2026-11-02T00:00:00+00:00"}))
    result = api.get("/api/v1/voice/endpoint/")
    assert result.data["server"] == "https://example-test.trycloudflare.com"
    assert result["Cache-Control"] == "no-store"
    for raw in ["broken", "[]", '{"server":"http://unsafe.example"}']:
        path.write_text(raw)
        assert api.get("/api/v1/voice/endpoint/").data["server"] == ""


def startup_module():
    spec = importlib.util.spec_from_file_location("startup_endpoint", Path(__file__).resolve().parents[1] / "deploy/voice-tunnel/save_endpoint.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_startup_atomic_save_has_only_address_and_time(tmp_path):
    module = startup_module()
    target = tmp_path / "current.json"
    module.save(target, "https://current.trycloudflare.com")
    data = json.loads(target.read_text())
    assert set(data) == {"server", "updated_at"}
    assert data["server"] == "https://current.trycloudflare.com"
    assert not target.with_suffix(".tmp").exists()


def test_startup_rejects_redirect_and_accepts_authentication_gate():
    module = startup_module()
    assert module.NoRedirect().redirect_request(None) is None
    with patch.object(module, "build_opener") as opener:
        for status in (401, 302, 404, 502):
            from email.message import Message

            opener.return_value.open.side_effect = HTTPError("https://example.org", status, "test", Message(), None)
            assert module.gateway_ready("https://example.trycloudflare.com") == (status == 401)
