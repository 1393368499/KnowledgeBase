"""离线评测专用 Django settings：**零外呼**、sqlite + Qdrant embedded 本地模式。

为什么需要它
------------
评测要在"没有 PostgreSQL / 没有 Qdrant server / 没有模型服务"的机器上可复现。
本模块在 ``import knowledgebase.settings`` **之前**把运行期目录与所有开关设成离线值，
再 star-import 一次正式 settings，最后对少数几个必须"压平"的项做显式覆盖。

用法
----
由 ``evals/kb_eval/harness.py::bootstrap()`` 负责：

1. 设 ``KB_EVAL_WORKDIR``（必须在仓库根目录内，见 ``knowledgebase/paths.py`` 的硬约束）；
2. ``DJANGO_SETTINGS_MODULE=offline_settings`` 并 ``django.setup()``。

手工等价命令::

    $env:KB_EVAL_WORKDIR = "<repo>/artifacts/evals/manual"
    $env:DJANGO_SETTINGS_MODULE = "offline_settings"
    python -c "import django; django.setup()"

注意
----
* ``KB_SKIP_DOTENV=1`` 是**必须**的：正式仓库里有 ``.env``，而
  ``knowledgebase.settings`` 用 ``load_dotenv(..., override=True)`` 读它 —— 不跳过就会把
  ``KB_QDRANT_MODE=server`` / 真实模型端点 / PG 口令盖到本模块的离线值上，
  评测会直接外呼真实服务。
* ``KB_MODEL_CALLS_DISABLED=1`` 是任务要求（真模型调用全关）。它同时被当作
  "稠密检索全局开关"（见 ``pipeline._dense_globally_disabled``），但
  ``pipeline.py:2038`` 在 **测试适配器开启** 时会刻意忽略这个开关，所以稠密路的
  确定性假向量照旧可用；分类等真模型入口另由注入的假客户端兜底。
* 要把稠密路真正关掉（``bootstrap(dense=False)``），单靠
  ``KB_SEARCH_DENSE_ENABLED=0`` 不够（同样会被适配器绕过），harness 会在
  **检索那一刻临时摘掉 KB_TEST_ADAPTER**，让 dense 走"已关闭"的正常降级分支。
"""

from __future__ import annotations

import os
from pathlib import Path

# evals/offline_settings.py -> evals/ -> 仓库根（与 config/paths.PROJECT_ROOT 同一个）
_EVALS_DIR: Path = Path(__file__).resolve().parent
#: 开发期 evals/ 可能还没拷进仓库（例如在 evals_build/ 里构建），
#: 允许用 KB_EVAL_REPO_ROOT 显式指定被测仓库根；正式交付时留空即可。
PROJECT_ROOT: Path = Path(
    os.environ.get("KB_EVAL_REPO_ROOT", "").strip() or str(_EVALS_DIR.parent)
).expanduser().resolve()

RUN_ID: str = os.environ.get("KB_EVAL_RUN_ID", "").strip() or "offline"
_DEFAULT_WORKDIR = PROJECT_ROOT / "artifacts" / "evals" / RUN_ID
WORKDIR: Path = Path(
    os.environ.get("KB_EVAL_WORKDIR", "").strip() or str(_DEFAULT_WORKDIR)
).expanduser().resolve()


def _inside_repo_root(path: Path) -> bool:
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    return True


if not _inside_repo_root(WORKDIR):
    raise RuntimeError(
        "KB_EVAL_WORKDIR 必须位于仓库根目录内（knowledgebase/paths.py 的 "
        f"assert_inside_project_root 硬约束）：{WORKDIR} 不在 {PROJECT_ROOT} 里。"
        "请把评测运行目录设为 <仓库根>/artifacts/evals/<run_id>。"
    )

# ---- 运行期目录重定向（必须在 import knowledgebase.settings 之前；它导入期就建目录）----
_DIR_ENV = {
    "VECTOR_DATA_DIR": ("vector_data", True),
    "RAW_STORE_DIR": ("raw", True),
    "MEDIA_ROOT_DIR": ("media", True),
    "DB_DIR": ("db", True),
}
for _var, (_sub, _force) in _DIR_ENV.items():
    _path = WORKDIR / _sub
    _path.mkdir(parents=True, exist_ok=True)
    if _force or not os.environ.get(_var):
        os.environ[_var] = str(_path)

# ---- 离线开关（同样必须在 import knowledgebase.settings 之前）----
os.environ["KB_SKIP_DOTENV"] = "1"        # 别让仓库里的 .env 盖掉离线值
os.environ["DJANGO_DEBUG"] = "1"          # 非 DEBUG + sqlite 会被 settings 直接拒绝
os.environ["DB_ENGINE"] = "sqlite"
os.environ["KB_TEST_ADAPTER"] = "offline"  # 确定性假向量；只允许写 __offline_test
os.environ["KB_TEST_ADAPTER_DIMENSION"] = os.environ.get("KB_TEST_ADAPTER_DIMENSION", "1024")
os.environ["KB_QDRANT_MODE"] = "embedded"  # Qdrant 本地嵌入模式，不连 server
os.environ["USE_REDIS_CACHE"] = "0"       # 缓存走 LocMem，不连 Redis
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"
os.environ["KB_UPLOAD_PROCESS_MODE"] = "inline"
os.environ["KB_DISPATCH_DISABLED"] = "1"  # 不派发后台任务
os.environ["KB_MODEL_LIMIT_ENABLED"] = "0"  # 模型并发闸门要 Redis，离线关掉
# 任务口径要求"模型调用全关"，这里照做：它同时是"稠密检索全局开关"，但
# pipeline.py:2038 在 **测试适配器开启** 时刻意忽略这个开关（让测试能覆盖稠密路），
# 所以稠密路的假向量照旧可用；分类/重排等真模型入口本来就靠注入假客户端或关闭来兜底。
os.environ["KB_MODEL_CALLS_DISABLED"] = "1"
os.environ.setdefault("KB_SEARCH_DENSE_ENABLED", "1")
os.environ["KB_SEARCH_USE_RERANK"] = "0"  # 离线没有重排服务：禁止外呼
os.environ.setdefault("KB_QDRANT_COLLECTION_PREFIX", "kb")

# ---- 正式 settings（此时目录与开关都已就位）----
from knowledgebase.settings import *  # noqa: E402,F401,F403  (star-import 是这里的目的)
from knowledgebase.settings import DATABASES as _BASE_DATABASES  # noqa: E402

# ============================ 显式覆盖（压平）============================
# 1) 数据库：固定到 workdir 内的 sqlite 文件（不碰 db/kb.sqlite3）
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(WORKDIR / "db" / "kb.sqlite3"),
        "OPTIONS": {"timeout": 20},
    }
}

# 2) 向量库：embedded 本地模式；URL 清空，杜绝任何指向 server 的机会
QDRANT_MODE = "embedded"
QDRANT_URL = ""

# 3) 离线适配器 + 稠密检索（前者写假向量，后者保持开启才能测 dense 路）
KB_TEST_ADAPTER = "offline"
KB_TEST_ADAPTER_DIMENSION = int(os.environ.get("KB_TEST_ADAPTER_DIMENSION", "1024"))
KB_MODEL_CALLS_DISABLED = True
KB_SEARCH_DENSE_ENABLED = os.environ.get("KB_SEARCH_DENSE_ENABLED", "1") == "1"
KB_SEARCH_USE_RERANK = False

# 4) 队列/缓存/派发全部进程内，避免任何网络连接
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_BEAT_SCHEDULE = {}
KB_DISPATCH_DISABLED = True
KB_UPLOAD_PROCESS_MODE = "inline"
KB_MODEL_LIMIT_ENABLED = False
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": f"kb-eval-{RUN_ID}",
    }
}

#: 评测运行目录（harness 之外的脚本也可以从这里取，避免两处算路径）
KB_EVAL_WORKDIR = str(WORKDIR)
