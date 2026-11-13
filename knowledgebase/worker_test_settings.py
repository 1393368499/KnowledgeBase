"""真实 worker 子进程专用 settings：**显式、fail-closed、禁连 AS711**。

为什么不能只是"import knowledgebase.settings 再改 DB NAME"
----------------------------------------------------
``knowledgebase.settings`` 里有 ``load_dotenv(PROJECT_ROOT/".env", override=True)``。
``override=True`` 意味着 **.env 会盖掉进程环境变量**，于是子进程传进来的
broker / 模型开关 / 嵌入桩地址会被 .env 里的业务值覆盖——只改 DB NAME 根本
不够（审查指出的漏洞）。

所以顺序必须是：

1. **先**校验测试覆盖项，缺任何一项直接**拒绝启动**（fail-closed）；
2. **再**只把 ``.env`` 里的**连接凭据**（host/port/user/password）读进来；
3. 设 ``KB_SKIP_DOTENV=1``，让 ``knowledgebase.settings`` **不要**再加载 .env；
4. import ``knowledgebase.settings``；
5. 把第 1 步的覆盖**逐项写回** settings 对象（不依赖环境变量优先级）；
6. 断言"没有任何 AS711 端点被配置上"，否则拒绝启动。

结果：凭据来自 .env，其余一切都来自测试显式输入，且不可能意外连到 AS711。
"""

from __future__ import annotations

import json
import os

# ---------------------------------------------------------------------------
# 1) fail-closed：这些必须由测试提供，缺一个就拒绝启动
# ---------------------------------------------------------------------------
_REQUIRED = {
    "KB_WORKER_DB_NAME": "要连的测试库名（Django 实际在用的那个）",
    "KB_WORKER_BROKER_URL": "测试专用 broker（独立 Redis DB）",
    "KB_WORKER_QDRANT_PREFIX": "collection 命名空间前缀（测试专用）",
    "KB_WORKER_EMBED_BASE_URL": "本地嵌入桩地址（回环地址）",
}
_missing = [name for name in _REQUIRED if not os.environ.get(name, "").strip()]
if _missing:
    raise RuntimeError(
        "worker 测试设置缺少必需项，拒绝启动："
        + "；".join(f"{name}（{_REQUIRED[name]}）" for name in _missing)
    )

_DB_NAME = os.environ["KB_WORKER_DB_NAME"].strip()
_BROKER_URL = os.environ["KB_WORKER_BROKER_URL"].strip()
_COLLECTION_PREFIX = os.environ["KB_WORKER_QDRANT_PREFIX"].strip()
_EMBED_BASE_URL = os.environ["KB_WORKER_EMBED_BASE_URL"].strip()

# 嵌入桩必须在本机回环地址上：这是"绝不出内网"的第一道闸门
if not (
    _EMBED_BASE_URL.startswith("http://127.0.0.1")
    or _EMBED_BASE_URL.startswith("http://localhost")
):
    raise RuntimeError(f"嵌入桩地址必须是本机回环地址，拒绝启动：{_EMBED_BASE_URL}")

# ---------------------------------------------------------------------------
# 2) **连接配置全部由父进程显式传入；一行都不从 .env 读**
#
# 独立审查发现的真实失败：旧实现无条件把 `.env` 里的 `KB_PG_HOST/PORT`（业务值
# `127.0.0.1:55432`）写进环境变量，把父进程显式传的独占 `pg:5432` 盖掉 ——
# 子 worker 连错主机，任务全部因 PG 拒绝而失败。更严重的是**密码**：兜底读
# `.env` 等于让测试子进程去用生产密码。
#
# 现在的口径（fail-closed，无任何兜底）：
#   * `KB_PG_HOST/PORT/USER/PASSWORD` 四个键**必须由父进程显式给出**；
#   * 「显式」= 键存在于环境里（**空字符串也算**，允许 trust/peer 空密码）；
#   * 缺任何一个 → 拒绝启动，错误信息点名缺了哪些键；
#   * 不再读取 `.env` 的任何连接凭据（`KB_WORKER_DOTENV_PATH` 仅保留给测试注入）。
# ---------------------------------------------------------------------------
_CREDENTIAL_KEYS = ("KB_PG_HOST", "KB_PG_PORT", "KB_PG_USER", "KB_PG_PASSWORD")
_REQUIRED_CONN = {
    "KB_PG_HOST": "父进程显式提供的测试数据库主机（例如独占网络里的 pg）",
    "KB_PG_PORT": "父进程显式提供的测试数据库端口（例如 5432）",
    "KB_PG_USER": "父进程显式提供的测试数据库用户",
    "KB_PG_PASSWORD": "父进程显式提供的测试数据库密码（允许为空字符串，但必须传）",
}
_missing_conn = [f"{name}（{_REQUIRED_CONN[name]}）" for name in _CREDENTIAL_KEYS if name not in os.environ]
if _missing_conn:
    raise RuntimeError(
        "worker 测试设置缺少**显式父连接**配置，拒绝启动"
        "（绝不回读 .env 的业务连接/生产密码）：" + "；".join(_missing_conn)
    )
if not os.environ.get("KB_PG_HOST", "").strip() or not os.environ.get("KB_PG_PORT", "").strip():
    raise RuntimeError(
        "worker 测试设置的 KB_PG_HOST/KB_PG_PORT 不能为空：拒绝启动"
        "（空主机/端口只会连到默认实例，属于误连风险）"
    )
# 库名只能来自 KB_WORKER_DB_NAME：清掉可能误导的键
os.environ.pop("KB_PG_DB", None)
os.environ.pop("KB_PG_PASSWORD_FILE", None)
os.environ["DB_ENGINE"] = "postgres"

# ---------------------------------------------------------------------------
# 3) 让 knowledgebase.settings 不要再加载 .env（否则 override=True 会盖掉一切）
# ---------------------------------------------------------------------------
os.environ["KB_SKIP_DOTENV"] = "1"

# ---------------------------------------------------------------------------
# 3b) **清掉一切可能指向 AS711 的继承环境变量**
#
# 为什么必须清：``apps.knowledge.embedding.profiles()`` 组装 profile 时，
# 对默认 profile 会**优先读环境变量** ``AS711_EMBED_BASE_URL``（其次才是
# settings 的 MODEL_SERVICES）。只改 MODEL_SERVICES 不保证最终生效的
# base_url 是本机桩——父进程/宿主环境里的旧端点会把它盖回去，
# 那就等于"测试把向量发去了 AS711"。这里先物理清掉，最后再断言一次。
# ---------------------------------------------------------------------------
for _name in (
    "AS711_EMBED_BASE_URL",
    "AS711_EMBED_MODEL",
    "AS711_EMBED_DIM",
    "AS711_RERANK_BASE_URL",
    "AS711_RERANK_MODEL",
    "AS711_OCR_BASE_URL",
    "AS711_OCR_MODEL",
    "AS711_LLM_BASE_URL",
    "KB_LLM_BASE_URL",
):
    os.environ.pop(_name, None)
# 只保留本机桩这一条来源
os.environ["KB_EMBED_PROFILES"] = json.dumps(
    {"bge1024": {"base_url": _EMBED_BASE_URL}}, ensure_ascii=False
)

# 4) 继承业务配置（凭据已在环境里）
from knowledgebase.settings import *  # noqa: E402,F401,F403

# ---------------------------------------------------------------------------
# 4.5) **端点/目录必须显式绑定**（真实端到端复验发现的缺陷）
# ---------------------------------------------------------------------------
# 问题：以前只传 `KB_WORKER_QDRANT_PREFIX`，没传 Qdrant 端点。
# `knowledgebase.settings` 用 dotenv(override=True) 载入 .env，于是子 worker 拿到的是
# **主实例 56333**，而父进程已经改到隔离端点 56339 ——
# 结果"子 worker 往主实例写、父 finally 在隔离端点清"，既泄漏又误碰主实例。
# 所以这里在 star-import **之后**（唯一可靠位置）直接绑定并**校验**。
_QDRANT_URL_OVERRIDE = os.environ.get("KB_WORKER_QDRANT_URL", "").strip()
if not _QDRANT_URL_OVERRIDE:
    raise RuntimeError(
        "worker 测试设置缺少 KB_WORKER_QDRANT_URL —— 拒绝启动"
        "（否则子进程会继承 .env 的主实例端点，写到错误的地方）"
    )
# 与主实例默认端点（.env 里那个）必须不同：同端点意味着没隔离
_MAIN_QDRANT_HINT = "56333"
if _MAIN_QDRANT_HINT in _QDRANT_URL_OVERRIDE:
    raise RuntimeError(
        f"拒绝启动：KB_WORKER_QDRANT_URL={_QDRANT_URL_OVERRIDE} 指向主实例"
        f"（含 {_MAIN_QDRANT_HINT}）—— 子 worker 必须写隔离端点"
    )
QDRANT_URL = _QDRANT_URL_OVERRIDE          # noqa: F405
QDRANT_MODE = "server"                     # noqa: F405

_RAW_OVERRIDE = os.environ.get("KB_WORKER_RAW_STORE_DIR", "").strip()
if not _RAW_OVERRIDE:
    raise RuntimeError("worker 测试设置缺少 KB_WORKER_RAW_STORE_DIR —— 拒绝启动")
RAW_STORE_DIR = _RAW_OVERRIDE              # noqa: F405
os.environ["RAW_STORE_DIR"] = _RAW_OVERRIDE

_VECTOR_OVERRIDE = os.environ.get("KB_WORKER_VECTOR_DATA_DIR", "").strip()
if not _VECTOR_OVERRIDE:
    raise RuntimeError("worker 测试设置缺少 KB_WORKER_VECTOR_DATA_DIR —— 拒绝启动")
VECTOR_DATA_DIR = _VECTOR_OVERRIDE         # noqa: F405
os.environ["VECTOR_DATA_DIR"] = _VECTOR_OVERRIDE

# ---------------------------------------------------------------------------
# 5) 逐项写回测试覆盖
# ---------------------------------------------------------------------------
DATABASES["default"]["NAME"] = _DB_NAME  # noqa: F405

CELERY_BROKER_URL = _BROKER_URL  # noqa: F405
CELERY_RESULT_BACKEND = _BROKER_URL  # noqa: F405
CELERY_TASK_ALWAYS_EAGER = False  # noqa: F405
CELERY_TASK_DEFAULT_QUEUE = "kb-worker-test-default"  # noqa: F405
CELERY_BROKER_TRANSPORT_OPTIONS = {  # noqa: F405
    "socket_timeout": 3,
    "socket_connect_timeout": 3,
    "retry_on_timeout": False,
    "max_retries": 1,
}

# 向量集合落在测试专用命名空间
KB_QDRANT_COLLECTION_PREFIX = _COLLECTION_PREFIX  # noqa: F405
# 不开假向量适配器：worker 走"真实 HTTP + 本地桩"，两个进程对 collection 名
# 的理解才一致（适配器会给名字加后缀，而父进程未必开）
KB_TEST_ADAPTER = ""  # noqa: F405
# 允许调用——但只允许调用本机桩（下面还会断言没有 AS711 端点）
KB_MODEL_CALLS_DISABLED = False  # noqa: F405

# 嵌入指向本地桩；重排 / OCR / LLM **留空**（未配置 → 本地立刻失败，不外呼）
MODEL_SERVICES = dict(globals().get("MODEL_SERVICES", {}) or {})  # noqa: F405
_embedding = dict(MODEL_SERVICES.get("embedding", {}) or {})
_embedding["base_url"] = _EMBED_BASE_URL
MODEL_SERVICES["embedding"] = _embedding
for _service in ("rerank", "ocr", "llm"):
    _entry = dict(MODEL_SERVICES.get(_service, {}) or {})
    _entry["base_url"] = ""
    MODEL_SERVICES[_service] = _entry

KB_EMBED_PROFILES = os.environ.get("KB_EMBED_PROFILES", "")  # noqa: F405

# ---------------------------------------------------------------------------
# 6) fail-closed：确认没有任何 AS711 端点被配置上
# ---------------------------------------------------------------------------
_AS711_HOST = "172.16.100.114"
_leaks: list[str] = []
if _AS711_HOST in str(MODEL_SERVICES):
    _leaks.append("MODEL_SERVICES 仍包含 AS711")
if _AS711_HOST in str(KB_EMBED_PROFILES):
    _leaks.append("KB_EMBED_PROFILES 仍包含 AS711")
for _peer in ("MODEL_API_KEY",):
    if _AS711_HOST in str(globals().get(_peer, "")):
        _leaks.append(f"{_peer} 仍包含 AS711")
# worker 自己不会用到的统一端点配置
for _peer in ("KB_LLM_BASE_URL", "AS711_LLM_BASE_URL", "AS711_RERANK_BASE_URL", "AS711_OCR_BASE_URL"):
    if _AS711_HOST in str(globals().get(_peer, "")):
        _leaks.append(f"{_peer} 仍包含 AS711")
if _leaks:
    raise RuntimeError("worker 测试设置检测到 AS711 端点，拒绝启动：" + "；".join(_leaks))

# ---------------------------------------------------------------------------
# 7) 最终断言：**实际生效的 profile** 必须解析到本机桩
#
# 只检查 settings 变量是不够的——``embedding.profiles()`` 还有环境变量与
# cache 两层。这里直接问它"你到底会用哪个 base_url"，不一致就拒绝启动。
# ---------------------------------------------------------------------------
from apps.knowledge.embedding import active_profile as _active_profile  # noqa: E402

_effective = _active_profile()
if _effective.base_url.rstrip("/") != _EMBED_BASE_URL.rstrip("/"):
    raise RuntimeError(
        "worker 生效的嵌入端点不是本机桩，拒绝启动："
        f"active_profile().base_url={_effective.base_url!r} ≠ {_EMBED_BASE_URL!r}"
    )
if _AS711_HOST in _effective.base_url:
    raise RuntimeError(f"worker 生效的嵌入端点指向 AS711：{_effective.base_url}")
