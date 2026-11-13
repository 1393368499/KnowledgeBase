"""真实 worker 子进程 settings 的连接保护（独立审查发现的 PG 覆盖缺陷）。

旧行为：`knowledgebase/worker_test_settings.py` 无条件把 `.env` 的 `KB_PG_HOST/PORT`
写进环境 → 独占网络里父进程显式传的 `pg:5432` 被 `.env` 的 `127.0.0.1:55432`
盖掉，子 worker 连错主机，任务全部失败。

本文件用**子进程**验证（settings 模块的副作用必须在干净解释器里观察）：
  1. 父进程显式给 host/port/user 时，`.env`（哪怕是业务值）**不得覆盖**；
  2. 缺显式父连接配置 → **fail-closed** 拒绝启动，且错误信息点名缺了哪些键；
  3. `KB_PG_DB`（业务库名）被忽略：库名只认 `KB_WORKER_DB_NAME`。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PROBE = (
    "import django, json;"
    "django.setup();"
    "from django.conf import settings as s;"
    "print(json.dumps({'host': s.DATABASES['default']['HOST'],"
    " 'port': str(s.DATABASES['default']['PORT']),"
    " 'user': s.DATABASES['default']['USER'],"
    " 'password': s.DATABASES['default']['PASSWORD'],"
    " 'name': s.DATABASES['default']['NAME']}))"
)


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("KB_WORKER_", "KB_PG_", "AS711_"))
    }
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "knowledgebase.worker_test_settings",
            "PYTHONPATH": str(REPO),
            "PYTHONIOENCODING": "utf-8",
            "KB_WORKER_DB_NAME": "kb_worker_guard_test",
            "KB_WORKER_BROKER_URL": "redis://127.0.0.1:6379/14",
            "KB_WORKER_QDRANT_PREFIX": "kb-workerguard",
            "KB_WORKER_EMBED_BASE_URL": "http://127.0.0.1:9/v1",
            "KB_WORKER_QDRANT_URL": "http://127.0.0.1:6333",
            "KB_WORKER_RAW_STORE_DIR": str(tmp_path / "raw"),
            "KB_WORKER_VECTOR_DATA_DIR": str(tmp_path / "vec"),
        }
    )
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "vec").mkdir(exist_ok=True)
    return env


def _run(env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess:
    # 业务 .env 用临时文件替代（**不改仓库里的 .env**）
    dotenv = tmp_path / "env"
    dotenv.write_text(
        "KB_PG_HOST=127.0.0.1\nKB_PG_PORT=55432\nKB_PG_USER=business\nKB_PG_DB=kb\n",
        encoding="utf-8",
    )
    env["KB_WORKER_DOTENV_PATH"] = str(dotenv)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


@pytest.mark.skipif(os.name != "nt", reason="该保护在任意平台都成立，但这里的 .env 语义按 Windows 开发机验证")
def test_explicit_parent_connection_wins_over_dotenv(tmp_path):
    env = _base_env(tmp_path)
    env.update({"KB_PG_HOST": "pg", "KB_PG_PORT": "5432", "KB_PG_USER": "kb_test"})
    # 密码显式传**空字符串**（trust/peer）：必须被接受，且不许回落 .env 的生产密码
    env["KB_PG_PASSWORD"] = ""
    result = _run(env, tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["host"] == "pg", payload
    assert payload["port"] == "5432", payload
    assert payload["user"] == "kb_test", payload
    # 空密码被接受，且**不是** .env 里那个业务密码
    assert payload["password"] == "", payload
    # 库名只认 KB_WORKER_DB_NAME（.env 里的 KB_PG_DB 被忽略）
    assert payload["name"] == "kb_worker_guard_test", payload


def test_missing_explicit_password_key_fails_closed(tmp_path):
    """**密码键缺失**（不是空）必须拒绝启动 —— 那正是"回读生产密码"的入口。"""
    env = _base_env(tmp_path)
    env.update({"KB_PG_HOST": "pg", "KB_PG_PORT": "5432", "KB_PG_USER": "kb_test"})
    env.pop("KB_PG_PASSWORD", None)
    result = _run(env, tmp_path)
    assert result.returncode != 0, "缺 KB_PG_PASSWORD 键时必须拒绝，而不是回读 .env 的生产密码"
    assert "KB_PG_PASSWORD" in (result.stdout + result.stderr)


def test_missing_explicit_connection_fails_closed(tmp_path):
    env = _base_env(tmp_path)  # 故意不给 KB_PG_HOST/PORT/USER/PASSWORD
    result = _run(env, tmp_path)
    assert result.returncode != 0, "缺少显式父连接配置时必须拒绝启动"
    combined = (result.stdout + result.stderr)
    for key in ("KB_PG_HOST", "KB_PG_PORT", "KB_PG_USER", "KB_PG_PASSWORD"):
        assert key in combined, combined[-800:]
