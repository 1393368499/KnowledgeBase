"""测试专用配置：让测试在**基础设施不可用时也能跑**。

背景（用户 2026-10-13 指示）
----------------------------
AS711 会长时间断开，本机的 Postgres/Qdrant 容器也可能被重启。开发不能因此停摆，
但也不能"假装测过了"。这里的口径是：

- **默认**跑 SQLite（Django 会建一个独立的内存/临时测试库），
  所以单测在完全离线（没网、没容器、没 AS711）时也能全绿；
- 想验证真实 PostgreSQL 行为时显式指定：``pytest --kb-db=postgres``；
  连不上就直接**报错退出**，绝不悄悄降级成 SQLite 然后宣称"在 Postgres 上过了"；
- 真实 Qdrant 的用例统一用 ``--kb-qdrant`` / 环境变量门控，连不上就 skip 并
  在报告里写明 skip 原因，不把 skip 当 pass。

为什么单独一个文件而不改 ``config/settings.py``：生产口径必须是
"非 DEBUG 禁 SQLite"（那是 P1 的硬约束），测试口径允许 SQLite——
把这层差异锁在测试配置里，生产配置一行不动。
"""

from __future__ import annotations

import os
import socket
import sys

# ------------------------------------------------- 会话级测试隔离（必须最先）----
# 为什么放在这里而不是 tests/conftest.py：**config.settings 在导入时**就用
# os.environ.get(...) 取了快照。conftest 执行更晚，设了也不生效（实测：
# ENV=kb-t<uuid> 但 settings 里仍是 kb）→ 测试会去碰主实例历史集合（kb-1-g1）。
# 因此必须在 `from config.settings import *` **之前**设好。
import os as _os  # noqa: E402
import uuid as _uuid  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_PROJECT_ROOT = _Path(__file__).resolve().parent.parent
#: 本次测试会话的 run_uuid：conftest 会复用它，保证 raw 目录与资源登记一致
_TEST_RUN_UUID = _os.environ.get("KB_TEST_RUN_UUID") or _uuid.uuid4().hex[:12]
_os.environ["KB_TEST_RUN_UUID"] = _TEST_RUN_UUID
#: 测试期间原件只落本 run 的隔离目录，绝不写项目 media/raw
_TEST_RAW_DIR = _PROJECT_ROOT / "artifacts" / "test-runs" / _TEST_RUN_UUID / "raw"
_TEST_RAW_DIR.mkdir(parents=True, exist_ok=True)
_os.environ["RAW_STORE_DIR"] = str(_TEST_RAW_DIR)
#: 同理：公文模板的原件（``<media>/templates``）也必须落在本 run 的隔离目录里。
#: 否则单测会把用户模板写进项目 ``media/`` —— 那是"测试污染真实运行目录"，
#: 与 ``RAW_STORE_DIR`` 的隔离口径必须一致。
_TEST_MEDIA_DIR = _PROJECT_ROOT / "artifacts" / "test-runs" / _TEST_RUN_UUID / "media"
_TEST_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
_os.environ["MEDIA_ROOT_DIR"] = str(_TEST_MEDIA_DIR)
#: 向量前缀必须与会话唯一值一致，否则会与主实例的 kb-* 撞名
# 必须与 RunResources.prefix（f"kb-{uuid}-"）对齐：collection_name() 会在其后
# 补 "-<library_id>-g<generation>"，于是真实集合名 kb-<uuid>-1-g1 正好落在
# 本 run 的独占前缀之内；之前写成 kb-t<uuid> 会**清不掉**（前缀不匹配）。
_os.environ["KB_QDRANT_COLLECTION_PREFIX"] = f"kb-{_TEST_RUN_UUID}"

# **端点一致性**（真实泄漏根因）：session 清理用 KB_TEST_QDRANT_URL，但
# `vectorindex.build_client()` 读的是 settings.QDRANT_URL —— 若后者仍继承主实例
# (56333)，就会出现"在一个端点建集合、在另一个端点清"→ 清不掉、甚至误碰主实例。
# 所以这里**显式绑定**：有隔离端点就指过去；没有就退回 embedded 隔离，
# 绝不在共享 server 上建集合（建了也清不掉）。
# 注意优先级：config/settings.py 用的是
#   QDRANT_URL  = _env_first("KB_QDRANT_URL", "QDRANT_URL", ...)
#   QDRANT_MODE = _env_first("KB_QDRANT_MODE", ...)
# 即 **KB_QDRANT_URL / KB_QDRANT_MODE 优先**。只设 QDRANT_URL/QDRANT_MODE 会被
# .env 里已有的主实例值盖住（实测：设了 QDRANT_URL=56339 结果仍是 56333）。
# 所以必须写高优先级的名字。
_TEST_QDRANT_URL = _os.environ.get("KB_TEST_QDRANT_URL", "").strip()
if _TEST_QDRANT_URL:
    _os.environ["KB_QDRANT_URL"] = _TEST_QDRANT_URL    # 高优先级，覆盖 .env
    _os.environ["KB_QDRANT_MODE"] = "server"
else:
    # 没有隔离端点 → **不得**使用共享 server（建了集合也清不掉、还可能碰主实例）
    _os.environ.pop("KB_QDRANT_URL", None)
    _os.environ["KB_QDRANT_MODE"] = "embedded"

from config.settings import *  # noqa: F403,F401 - 继承生产配置
from config.settings import DATABASES

# ``ensure_runtime_dirs()`` 在 config.settings 导入时就按环境变量建好了目录；
# 这里再**显式钉一次** MEDIA_ROOT，防止 .env 里的旧值（若有）把隔离目录盖回去。
MEDIA_ROOT = _os.environ["MEDIA_ROOT_DIR"]
_os.makedirs(_os.path.join(MEDIA_ROOT, "templates"), exist_ok=True)

# ------------------------------------------------- 端点/模式**强制覆盖** ----
# config.settings 用 dotenv(**override=True**) 加载 .env，会用 .env 里的主实例值
# 盖掉我们先前设的环境变量（实测：环境变量设了 56339，settings.QDRANT_URL 仍是
# http://127.0.0.1:56333）。所以必须在 star-import **之后**直接改模块级属性 ——
# 这是唯一可靠的位置。否则会出现"在一个端点建集合、在另一个端点清"→ 泄漏。
if _TEST_QDRANT_URL:
    QDRANT_URL = _TEST_QDRANT_URL      # 隔离端点：测试只许碰它
    QDRANT_MODE = "server"
else:
    QDRANT_MODE = "embedded"           # 无隔离端点 → 绝不用共享 server

# **向量数据目录也必须隔离**：默认是 `<项目根>/vector_data`，嵌入模式下会把索引
# 写进**主 vector_data/embedded**（与生产/主实例混在一起）。指到本 run 目录下。
os.environ["VECTOR_DATA_DIR"] = str(_PROJECT_ROOT / "artifacts" / "test-runs"
                                    / _TEST_RUN_UUID / "vector_data")
VECTOR_DATA_DIR = _os.environ["VECTOR_DATA_DIR"]
# **必须在这里就建出来**：`/health/ready` 会检查该目录是否存在，而健康检查
# 可能早于任何上传发生（实测：不预建则 ready 返回 503）。
# 这属于"就绪初始化"，不能靠把健康检查改成接受 503 或 mock 掉来绕过。
_os.makedirs(VECTOR_DATA_DIR, exist_ok=True)
_os.makedirs(_os.path.join(VECTOR_DATA_DIR, "embedded"), exist_ok=True)

# ------------------------------------------------------- 测试口径的硬覆盖 ----
#
# **离线模式必须在测试里强制关闭。**
# 踩过的坑：本地 .env 为了做界面验收设了 ``KB_MODEL_CALLS_DISABLED=1``，
# 而 settings 用 ``load_dotenv(override=True)``，于是**所有测试**都变成"离线模式"，
# 大批用 MockTransport 验证真实外呼路径的用例直接失败（15 条）。
# 测试要的是可预测的环境，不是"跟随开发者本机验收开关"。
#
# 需要验证离线行为的用例请自己 ``@override_settings(KB_MODEL_CALLS_DISABLED=True)``。
KB_MODEL_CALLS_DISABLED = False
KB_SEARCH_DENSE_ENABLED = True
# 同样强制：不允许本机 .env 把"假向量适配器"打开后再跑全量测试
# （需要它的用例由 conftest 的 isolated_paths 显式打开）。
KB_TEST_ADAPTER = ""
# 上传处理模式在测试里也要可预测：具体用例各自 override，默认按请求内处理。
KB_UPLOAD_PROCESS_MODE = os.environ.get("KB_UPLOAD_PROCESS_MODE", "auto")
# **离线回归一律用进程内熔断后端**（用户/独立审查的硬要求）：默认的共享 Redis
# 后端会把测试里 MockTransport 造出来的假故障写进真实共享状态 —— 实测污染过
# DB14 的 `kb:breaker:*`（3 个键：__services__ / ocr / rerank，failures=3 的假 503）。
# 需要验证共享后端的用例自己 override（并有独占 DB/前缀），普通回归不碰 Redis。
KB_BREAKER_STORE = os.environ.get("KB_BREAKER_STORE", "process")

# **模型并发闸门（SlotLimiter）在测试里默认关闭**（用户 2026-10-17 指出的隔离缺口）。
#
# 为什么必须显式关掉：熔断后端早就改成进程内了，但闸门**没有** ——
# ``build_limiter()`` 在 ``KB_MODEL_LIMIT_REDIS_URL`` 为空时回落到
# ``settings.CELERY_BROKER_URL``，也就是**业务 Redis**。于是一条只用了
# ``MockTransport``（根本没发真外呼）的用例，只要路径上经过 ``slots("embedding")``，
# 就会去连业务 Redis 并在共享键空间里 ``zadd``/``zrem`` 槽位键 ——
# 那是"单测写真实共享状态"，和熔断器当初被污染（DB14 假 failures=3）是同一类缺陷。
# 注意它**不会**因为"没外呼"而幸免：闸门是在外呼**之前**取额度的。
#
# 需要验证真实限流的用例必须**自己显式启用**，并且用 FakeRedis 或独占地址/前缀
# （见 ``tests/test_cross_process_rate_limit.py``：它直接构造 ``SlotLimiter``，
# 指向测试专用 DB15 + 每次唯一的 ``kb:test:model:slots:<uuid>`` 前缀，
# 所以不受这里影响，也不会碰业务库 DB0）。
# 生产配置一行不动：``config/settings.py`` 仍是默认启用。
KB_MODEL_LIMIT_ENABLED = False
KB_PROGRESS_ENABLED = False  # 普通离线回归不向业务 Redis 写进度。
#: 连 URL 也显式清空：万一将来有代码在 enabled 之前就构造客户端，
#: 也不该从 CELERY_BROKER_URL 推出业务地址。
KB_MODEL_LIMIT_REDIS_URL = ""

# 说明：测试**不允许**向业务队列投递消息（审查意见 22:50）。
# 实现方式放在 tests/conftest.py 的 autouse 夹具里（替换 dispatch_processing），
# 而不是在这里改 broker——因为 `config/celery.py` 的 app 在测试配置生效**之前**
# 就已按 business settings 建好了，事后改 conf 并不总能真正重定向连接
# （实测：只改 conf 时 connection_for_write 仍解析到 127.0.0.1:6379/0）。
# 调用点替换更可靠：测试里不会有真 broker 调用，真实 worker 用例自己用独立 DB。

# ------------------------------------------------------------------ 数据库 ----


def _tcp_reachable(host: str, port: int, *, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _requested_db() -> str:
    """测试库类型。优先级：``KB_PYTEST_DB`` > ``KB_TEST_DB`` > ``--kb-db`` > 默认 ``sqlite``。

    **为什么优先读 ``KB_PYTEST_DB``**：``config/settings.py`` 会执行
    ``load_dotenv(.env, override=True)``，任何已在环境里的 *同名* 变量都会被 .env
    覆盖。所以本仓库统一用"**.env 里不存在的名字**"（``KB_PYTEST_DB``）来传递这个
    开关，并在 conftest 里同时认 ``--kb-db``。

    **为什么这里还要读 ``sys.argv``**（实测踩到的测试基础设施缺陷）：pytest-django
    在 ``pytest_load_initial_conftests`` 阶段（``tryfirst``）就加载了 Django settings，
    比 conftest 里设置环境变量更早 —— 于是 ``pytest --kb-db=postgres`` 会**静默跑
    SQLite**，报告却写着"已按 Postgres 验证"。settings 模块被导入时 ``sys.argv``
    已经就绪，所以直接在这里解析命令行是最可靠的兜底（环境变量仍然优先）。
    """
    for name in ("KB_PYTEST_DB", "KB_TEST_DB"):
        raw = os.environ.get(name, "").strip().lower()
        if raw:
            return raw
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        value = ""
        if arg == "--kb-db" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--kb-db="):
            value = arg.split("=", 1)[1]
        value = value.strip().lower()
        if value:
            return value
    return "sqlite"


def _postgres_params() -> dict:
    """测试用 PostgreSQL 连接参数。

    测试库名**默认与主库不同名**（``<主库>_pytest``）：审查方与本机其它会话可能
    同时在跑，若都去抢 Django 默认的 ``test_<主库>`` 就会互相干扰
    （曾经真的撞到 ``Database "test_kb" is being accessed by other users``）。
    可用 ``KB_PG_TEST_DB`` 显式指定，便于并行跑多套。
    """
    main_name = os.environ.get("KB_PG_DB", "kb")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("KB_PG_TEST_DB", f"{main_name}_pytest"),
        "USER": os.environ.get("KB_PG_USER", "kb"),
        "PASSWORD": os.environ.get("KB_PG_PASSWORD", ""),
        "HOST": os.environ.get("KB_PG_HOST", "127.0.0.1"),
        "PORT": os.environ.get("KB_PG_PORT", "55432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 2},
        "TEST": {
            # 再叠一层区分：Django 会在这里前面加 "test_"
            "NAME": os.environ.get("KB_PG_TEST_DB_NAME", f"{main_name}_pytest_run"),
        },
    }


def _sqlite_params() -> dict:
    return {
        "ENGINE": "django.db.backends.sqlite3",
        # 默认内存库：单测全离线、跑完即消失，不落任何文件、不碰真实数据。
        # 需要"从零安装迁移链"的可复查证据时，由 `manage.py kbcheckinstall` 用
        # 临时文件库单独跑一遍（它会把路径打印出来）。
        "NAME": os.environ.get("KB_SQLITE_TEST_PATH") or ":memory:",
        "OPTIONS": {"timeout": 20},
    }


_REQUESTED = _requested_db()
_PG = _postgres_params()
_PG_UP = _tcp_reachable(_PG["HOST"], _PG["PORT"])

if _REQUESTED == "postgres":
    if not _PG_UP:
        raise RuntimeError(
            f"KB_TEST_DB=postgres 但 {_PG['HOST']}:{_PG['PORT']} 连不上。"
            "请先 `docker compose up -d`，或去掉 KB_TEST_DB 跑默认的离线（SQLite）测试。"
            "（这里故意直接报错：不允许悄悄降级成 SQLite 还宣称在 Postgres 上验证过。）"
        )
    DATABASES = {"default": _PG}  # noqa: F405
    KB_TEST_DB_KIND = "postgres"
elif _REQUESTED == "auto":
    # 显式要求自动探测（团队里有人想"有 PG 就顺手用 PG"时用）
    if _PG_UP:
        DATABASES = {"default": _PG}  # noqa: F405
        KB_TEST_DB_KIND = "postgres"
    else:
        DATABASES = {"default": _sqlite_params()}  # noqa: F405
        KB_TEST_DB_KIND = "sqlite"
else:
    # 默认：SQLite。理由：单测必须**离线且确定**。
    # 用它当默认，是因为"有 Postgres 就自动用它"会让测试成败取决于
    # 本机容器状态（我们就被 test_kb 残留连接坑过一次）。
    # 要验证真实 PostgreSQL 行为请显式：pytest --kb-db=postgres
    DATABASES = {"default": _sqlite_params()}  # noqa: F405
    KB_TEST_DB_KIND = "sqlite"

# 测试里不走真实 Redis/Celery（离线可跑）；缓存用进程内
USE_REDIS_CACHE = False  # noqa: F405
CACHES = {  # noqa: F405
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "kb-tests",
    }
}
CELERY_TASK_ALWAYS_EAGER = True  # noqa: F405

# 测试不要求真实模型服务：AS711 相关的真实调用统一由显式开关门控
KB_TEST_ADAPTER = os.environ.get("KB_TEST_ADAPTER", "").strip().lower()

# 密码哈希提速（测试专用，生产用默认）
VOICE_AUDIO_ROOT = str(_TEST_MEDIA_DIR / "voice-audio")
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # noqa: F405
