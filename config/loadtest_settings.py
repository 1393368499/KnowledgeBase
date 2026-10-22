"""**负载测试专用** settings：fail-closed，且**不能被 ``.env`` 盖回业务配置**。

为什么需要独立模块
------------------
``config/settings.py`` 用 ``load_dotenv(PROJECT_ROOT/".env", override=True)``，
**override=True 意味着文件值会覆盖环境变量**。所以"在命令前设
``KB_PG_DB=xxx``"是**不生效**的。压测最危险的后果是把压力打到业务库、
业务队列，甚至打到 AS711。

做法（先导入、**再强制改写**，让 .env 无论写什么都无法翻盘）
-----------------------------------------------------------
1. 正常导入 ``config.settings``（这样数据库连接参数仍来自 ``.env``，
   不必把口令复制到命令行）；
2. 然后**直接改写 settings 模块属性**：库名、向量前缀、Redis DB、
   模型禁用、测试标识、禁止派发。此后任何来源都改不回去；
3. **校验**：隔离参数必须是压测专用值，且与业务默认值不同，否则**拒绝启动**；
4. 缺必需项也**拒绝启动**（fail-closed，不用默认值凑合）；
5. 最后做一次**落地断言**：确认改写真的生效了。

用法
----
::

    # 自动生成一套合规参数（推荐）
    .\\.venv\\Scripts\\python.exe scripts/loadtest_env.py

    $env:KB_LOADTEST_DB_NAME = 'kb_loadtest_<随机>'
    $env:KB_LOADTEST_QDRANT_PREFIX = 'kb-lt-<随机>'
    $env:KB_LOADTEST_BROKER_DB = '14'          # 独立 Redis DB，远离业务 DB0
    $env:DJANGO_SETTINGS_MODULE = 'config.loadtest_settings'

    .\\.venv\\Scripts\\python.exe manage.py migrate --no-input
    .\\.venv\\Scripts\\python.exe manage.py runserver 127.0.0.1:8011 --noreload
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

# ---- 1) 正常导入业务 settings（连接参数来自 .env，但下面都会被改写）----
from .settings import *  # noqa: F403
from . import settings as _settings

# ---- 2) 取参数并校验（缺项/撞业务值都拒绝启动）----
_DB_NAME = os.environ.get("KB_LOADTEST_DB_NAME", "").strip()
_QDRANT_PREFIX = os.environ.get("KB_LOADTEST_QDRANT_PREFIX", "").strip()
_BROKER_DB = os.environ.get("KB_LOADTEST_BROKER_DB", "").strip()
_LABEL = os.environ.get("KB_LOADTEST_LABEL", "").strip() or "loadtest"

_BUSINESS_DB = os.environ.get("KB_LOADTEST_FORBIDDEN_DB", "kb")
_BUSINESS_PREFIX = os.environ.get("KB_LOADTEST_FORBIDDEN_PREFIX", "kb")
_BUSINESS_BROKER_DB = os.environ.get("KB_LOADTEST_FORBIDDEN_BROKER_DB", "0")

_problems: list[str] = []
if not _DB_NAME:
    _problems.append("缺少 KB_LOADTEST_DB_NAME（独立测试库名）")
if not _QDRANT_PREFIX:
    _problems.append("缺少 KB_LOADTEST_QDRANT_PREFIX（随机向量命名空间前缀）")
if not _BROKER_DB:
    _problems.append("缺少 KB_LOADTEST_BROKER_DB（独立 Redis DB，例如 14）")
if _DB_NAME and _DB_NAME == _BUSINESS_DB:
    _problems.append(
        f"KB_LOADTEST_DB_NAME 不能等于业务库 {_BUSINESS_DB!r}（会写业务数据）"
    )
if _DB_NAME and _DB_NAME.startswith("test_"):
    _problems.append("KB_LOADTEST_DB_NAME 不要以 test_ 开头（避免与 pytest 库混淆）")
if _QDRANT_PREFIX and _QDRANT_PREFIX == _BUSINESS_PREFIX:
    _problems.append(
        f"KB_LOADTEST_QDRANT_PREFIX 不能等于业务前缀 {_BUSINESS_PREFIX!r}（会写业务向量集合）"
    )
import re as _re

# 前缀必须与 **run-id 绑定**：`kb-<12/32 位小写 hex>`。
# 以前要求 `kb-lt-`（共享家族）—— 它不属于任何 run，清理器按设计拒绝碰，
# 正是历史 42 GiB 里 125 个 `kb-lt-*` 清不掉的成因。
# 注意：这条必须与 `scripts/loadtest_env.py` 产出的前缀、
# `load_probe.assert_environment` 的校验、`run_resources` 的回收口径**保持一致**，
# 否则会出现"loadtest_env 生成的环境被本文件拒绝"的自相矛盾（真实踩过）。
_RUN_PREFIX_RE = _re.compile(r"^kb-([0-9a-f]{12}|[0-9a-f]{32})$")
if _QDRANT_PREFIX and not _RUN_PREFIX_RE.match(_QDRANT_PREFIX):
    _problems.append(
        f"KB_LOADTEST_QDRANT_PREFIX 必须是 'kb-<run_id>'（run_id 为 12/32 位小写 hex），"
        f"实际 {_QDRANT_PREFIX!r}；不再接受共享的 kb-lt-* 前缀（无法按 run 回收）"
    )
if _BROKER_DB and _BROKER_DB == _BUSINESS_BROKER_DB:
    _problems.append(
        f"KB_LOADTEST_BROKER_DB 不能是业务 Redis DB {_BUSINESS_BROKER_DB}（会派任务进业务队列）"
    )

if _problems:
    sys.stderr.write("负载测试配置被拒绝（fail-closed）：\n")
    for item in _problems:
        sys.stderr.write(f"  - {item}\n")
    sys.stderr.write(
        "\n提示：运行 .\\.venv\\Scripts\\python.exe scripts/loadtest_env.py 生成一套合规参数。\n"
    )
    raise SystemExit(2)

# ---- 3) 强制改写（关键：这一步之后 .env 再也影响不到这些项）----
_redis_url = (
    f"redis://{os.environ.get('KB_LOADTEST_REDIS_HOST', '127.0.0.1')}:"
    f"{os.environ.get('KB_LOADTEST_REDIS_PORT', '56379')}/{_BROKER_DB}"
)
_SETTINGS_OVERRIDES = {
    "KB_QDRANT_COLLECTION_PREFIX": _QDRANT_PREFIX,
    "KB_MODEL_CALLS_DISABLED": True,   # 压测绝不能打到 AS711
    "KB_LOAD_TEST_MODE": True,          # /guide/ 据此声明 load_test_mode
    "KB_LOAD_TEST_LABEL": _LABEL,
    "KB_DISPATCH_DISABLED": True,       # 禁止把任务派发到队列
    "CELERY_TASK_ALWAYS_EAGER": False,
    "CELERY_BROKER_URL": _redis_url,
    "CELERY_RESULT_BACKEND": _redis_url,
    "KB_REDIS_URL": _redis_url,
    # 压测测的是 HTTP 层并发与稳定性；inline 会让上传在请求内跑完流水线，
    # 那测的就不是接口并发了。用 queue，且因禁止派发，任务不会真进队列。
    "KB_UPLOAD_PROCESS_MODE": os.environ.get("KB_LOADTEST_UPLOAD_MODE", "queue"),
    # 不用测试适配器：压测要真实向量库路径；模型禁用会让嵌入如实失败并降级关键字，
    # 这正是"模型不可用时系统表现"的观测点。
    "KB_TEST_ADAPTER": "",
}
for _key, _value in _SETTINGS_OVERRIDES.items():
    setattr(_settings, _key, _value)

# 数据库：只改**库名**，连接参数（host/port/user/密码）沿用 .env
_settings.DATABASES["default"] = dict(_settings.DATABASES["default"])
_settings.DATABASES["default"]["NAME"] = _DB_NAME
# 压测库是现成的，不允许 Django 再建/销毁它
_settings.DATABASES["default"]["TEST"] = {"NAME": f"test_{_DB_NAME}"}

# 让 `from config.loadtest_settings import *` 也能拿到改写后的值
KB_QDRANT_COLLECTION_PREFIX = _QDRANT_PREFIX  # noqa: F405
KB_MODEL_CALLS_DISABLED = True
KB_LOAD_TEST_MODE = True
KB_LOAD_TEST_LABEL = _LABEL
KB_DISPATCH_DISABLED = True
KB_UPLOAD_PROCESS_MODE = _SETTINGS_OVERRIDES["KB_UPLOAD_PROCESS_MODE"]
KB_TEST_ADAPTER = ""

# ---------------------------------------------------------------------------
# **压测上传的原件必须落在本 run 的隔离目录**（真实复验发现的缺口）
# ---------------------------------------------------------------------------
# 以前这里没绑 RAW_STORE_DIR / VECTOR_DATA_DIR：`from .settings import *` 已经用
# dotenv(override=True) 把 raw 指向**主 media/raw**，于是真实压测上传会把原件
# 写进生产目录 —— 而客户端收尾只删向量，原件永远留下（正是 42 GiB 同类问题的成因）。
# 这里在 star-import **之后**（唯一可靠位置）强制绑定，并 fail-closed 校验。
_RAW_DIR = os.environ.get("KB_LOADTEST_RAW_STORE_DIR", "").strip()
_VECTOR_DIR = os.environ.get("KB_LOADTEST_VECTOR_DATA_DIR", "").strip()
_ENDPOINT = os.environ.get("KB_LOADTEST_QDRANT_URL", "").strip()
_problems: list[str] = []
if not _RAW_DIR:
    _problems.append("缺少 KB_LOADTEST_RAW_STORE_DIR（压测原件隔离目录）")
if not _VECTOR_DIR:
    _problems.append("缺少 KB_LOADTEST_VECTOR_DATA_DIR（压测向量目录）")
if not _ENDPOINT:
    _problems.append("缺少 KB_LOADTEST_QDRANT_URL（隔离端点）")
if _ENDPOINT and "56333" in _ENDPOINT:
    _problems.append(f"KB_LOADTEST_QDRANT_URL={_ENDPOINT} 指向主实例 —— 拒绝压测")
# 隔离目录必须在项目 root 内（沿用 paths 的唯一真源约束），且不能是主 media/raw
if _RAW_DIR:
    _raw_path = Path(_RAW_DIR).resolve()
    _root = Path(__file__).resolve().parent.parent
    if _root not in _raw_path.parents:
        _problems.append(f"RAW_STORE_DIR 必须在项目 root 内：{_raw_path}")
    if _raw_path == (_root / "media" / "raw").resolve():
        _problems.append("RAW_STORE_DIR 不能指向主 media/raw（会污染生产原件）")
if _problems:
    raise RuntimeError(
        "压测设置不合格，拒绝启动：" + "；".join(_problems)
        + "（请用 scripts/loadtest_env.py 生成的整套环境变量）"
    )

RAW_STORE_DIR = _RAW_DIR          # noqa: F405
VECTOR_DATA_DIR = _VECTOR_DIR     # noqa: F405
QDRANT_URL = _ENDPOINT            # noqa: F405
QDRANT_MODE = "server"            # noqa: F405
os.environ["RAW_STORE_DIR"] = _RAW_DIR
os.environ["VECTOR_DATA_DIR"] = _VECTOR_DIR
os.environ["KB_QDRANT_URL"] = _ENDPOINT
Path(_RAW_DIR).mkdir(parents=True, exist_ok=True)
Path(_VECTOR_DIR).mkdir(parents=True, exist_ok=True)
CELERY_BROKER_URL = _redis_url  # noqa: F405
CELERY_RESULT_BACKEND = _redis_url  # noqa: F405
DATABASES = _settings.DATABASES  # noqa: F405


# ---- 4) 落地断言：确认改写真的生效（而不是"我以为生效了"）----
def _assert_isolated() -> None:
    actual_db = _settings.DATABASES["default"]["NAME"]
    if actual_db != _DB_NAME:
        raise SystemExit(
            f"负载测试配置被拒绝：期望库 {_DB_NAME!r}，实际 {actual_db!r}（有别的来源覆盖了）"
        )
    if _settings.DATABASES["default"].get("ENGINE", "").endswith("sqlite3"):
        raise SystemExit(
            "负载测试配置被拒绝：落到了 SQLite。压测必须用真实 PostgreSQL"
            "（SQLite 的写并发语义与生产不同，测出来的数字没有意义）"
        )
    if _settings.KB_QDRANT_COLLECTION_PREFIX != _QDRANT_PREFIX:
        raise SystemExit("负载测试配置被拒绝：向量前缀改写未生效")
    if not _settings.KB_MODEL_CALLS_DISABLED:
        raise SystemExit("负载测试配置被拒绝：KB_MODEL_CALLS_DISABLED 必须为 True")
    if not _settings.KB_LOAD_TEST_MODE:
        raise SystemExit("负载测试配置被拒绝：KB_LOAD_TEST_MODE 必须为 True")
    if not _settings.KB_DISPATCH_DISABLED:
        raise SystemExit("负载测试配置被拒绝：KB_DISPATCH_DISABLED 必须为 True")


_assert_isolated()
print(
    f"[loadtest] db={_DB_NAME} qdrant_prefix={_QDRANT_PREFIX} broker={_redis_url} "
    f"models=disabled dispatch=disabled label={_LABEL}",
    file=sys.stderr,
)
