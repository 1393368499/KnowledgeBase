"""Django 配置。

环境变量优先（`.env` 由 `python-dotenv` 自动加载），不把任何口令写进代码或仓库。
"""

from __future__ import annotations

import os
import secrets
import sys

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from config.paths import PROJECT_ROOT, ensure_runtime_dirs

# ``override=True``：.env 是本地真相，优先于进程环境变量。
#
# 唯一的例外是 ``KB_SKIP_DOTENV=1``：真实 worker 子进程的往返测试需要把
# broker/DB 换成**测试专用的独立实例**，而这里若还去加载业务 .env 就会把
# 测试的配置盖回去（表现为"worker 收不到任务"）。测试用
# ``config.worker_test_settings`` 打开这个开关并加载自己的临时 .env。
# 生产与本地开发都不会设这个变量，行为完全不变。
if os.environ.get("KB_SKIP_DOTENV") != "1":
    load_dotenv(PROJECT_ROOT / ".env", override=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_first(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量。

    同名概念有两套前缀：`KB_PG_*`（项目自己的名字，docker-compose 也用这套给容器
    做插值）和 `POSTGRES_*`（很多人机器上已有的通用前缀）。两套都认，但 `.env`
    里的值优先——因为 `.env` 是显式写给这个项目的。
    """
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip()
    return default


# ---- 大模型预算与超时（必须**真的**从环境变量读，否则 llm.py 的提示无法生效）----
# 实测依据：Qwen3.6 回答 4 个字用掉 239 completion_tokens（推理过程占大头），
# 所以预算不能按"输出字数"估。128 的预算会把正文挤成空串。
KB_LLM_TIMEOUT_SECONDS = float(os.environ.get("KB_LLM_TIMEOUT_SECONDS", "60"))
KB_LLM_MAX_TOKENS = int(os.environ.get("KB_LLM_MAX_TOKENS", "4096"))
# 撰写文档正文长，单独给更大的预算与更长的超时
KB_LLM_DOC_TIMEOUT_SECONDS = float(os.environ.get("KB_LLM_DOC_TIMEOUT_SECONDS", "180"))
KB_LLM_DOC_MAX_TOKENS = int(os.environ.get("KB_LLM_DOC_MAX_TOKENS", "8192"))
# 输入按 UTF-8 字节做保守预算，另留输出与消息格式的余量。
KB_LLM_CONTEXT_TOKENS = int(os.environ.get("KB_LLM_CONTEXT_TOKENS", "262144"))
KB_LLM_CONTEXT_RESERVE_TOKENS = int(os.environ.get("KB_LLM_CONTEXT_RESERVE_TOKENS", "4096"))
KB_LLM_DOC_SOURCE_BYTES = int(os.environ.get("KB_LLM_DOC_SOURCE_BYTES", "160000"))
# 一次提问里允许模型连续发起几轮工具调用（只读检索会自动执行并把结果回填给模型）。
# 为什么要有上限：模型偶尔会反复检索，没有上限会把一轮对话拖成无限循环；
# 上限之外还有"关掉工具再问一次"的兜底，保证最终仍给出正文回答。
# 注意：必须在这里真正定义，否则 `getattr(settings, "KB_LLM_MAX_TOOL_ROUNDS", 3)`
# 会永远拿默认值，环境变量设了也不生效（KB_LLM_TEMPERATURE 出过同样的问题）。
KB_LLM_MAX_TOOL_ROUNDS = max(1, int(os.environ.get("KB_LLM_MAX_TOOL_ROUNDS", "3")))

# ---- 采样模式：全系统统一 greedy（这是**客户端侧**的止血开关，别随手关）----
# 背景（实测 2026-10-19，证据与复现步骤见 docs/as711-sophgo-vllm-mixed-sampling-crash.md）：
# AS711 的算能 vLLM-TPU 引擎在**同一个 batch** 里同时收到 greedy 请求
# （temperature < 1e-5，含服务端默认 0.0）和 random 请求（temperature > 1e-5）时，
# 采样器会走到 `torch.where(..., out=...)`，而该后端未实现 `aten::where.self_out`
# → EngineCore fatal 退出，**整个模型服务**连不上（用户看到"助手不可用：Connection refused"）。
# 我们控制不了别人发什么，但能保证"我们自己这一半"永远是 greedy：
# 少一个 random 请求，混采批次就不会因为本系统而出现。
# 代价：回答不再有随机性（同一问题重复问结果一致）——拿"多样性"换"可用性"。
# 等厂家镜像修好后恢复随机采样：KB_LLM_GREEDY_ONLY=0（此时 KB_LLM_TEMPERATURE 才生效）。
KB_LLM_GREEDY_ONLY = _env_bool("KB_LLM_GREEDY_ONLY", True)
KB_LLM_TEMPERATURE = float(os.environ.get("KB_LLM_TEMPERATURE", "0.2"))


def _env_list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")

# ---- 生产安全基线（2026-10-22 审计收口）------------------------------------
#
# 为什么要在导入期硬校验：默认密钥是公开字面量。一旦以 DEBUG=0 部署却忘了设置
# DJANGO_SECRET_KEY，任何拿到本仓库的人都能伪造 session / CSRF 签名。这种配置
# 错误必须在启动时炸掉，而不是"带病上线"。
# 测试进程不承载真实会话：换一个进程内随机密钥即可，避免"跑测试要先配密钥"。
_INSECURE_DEV_KEY = "dev-only-insecure-key-change-me"
if SECRET_KEY == _INSECURE_DEV_KEY:
    if DEBUG:
        pass  # 本地开发：开箱即用，保持原样
    elif "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        SECRET_KEY = "pytest-only-" + secrets.token_urlsafe(32)
    else:
        raise ImproperlyConfigured(
            "DJANGO_DEBUG=0 时必须显式设置 DJANGO_SECRET_KEY：默认值 "
            "'dev-only-insecure-key-change-me' 是公开的开发占位密钥，用于生产等于"
            "把会话与 CSRF 签名交给所有人"
        )

# HTTPS 相关开关**默认关闭**：本系统常见部署是 127.0.0.1 明文回环（http://127.0.0.1:8800）。
# 对外暴露时必须显式打开 KB_HTTPS_ONLY=1，一次性配齐 cookie/CSRF/HSTS 与代理头；
# 只开一半比不开更危险（例如只开 SECURE_SSL_REDIRECT 却没告诉 Django 自己在 TLS 后面，
# 会变成无限重定向）。
KB_HTTPS_ONLY = _env_bool("KB_HTTPS_ONLY", False)
SECURE_SSL_REDIRECT = KB_HTTPS_ONLY
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if KB_HTTPS_ONLY else None
SESSION_COOKIE_SECURE = KB_HTTPS_ONLY
CSRF_COOKIE_SECURE = KB_HTTPS_ONLY
SECURE_HSTS_SECONDS = int(
    os.environ.get("KB_HSTS_SECONDS", "31536000" if KB_HTTPS_ONLY else "0")
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = KB_HTTPS_ONLY
SECURE_HSTS_PRELOAD = KB_HTTPS_ONLY
# 反代到别的域名时：KB_CSRF_TRUSTED_ORIGINS=https://kb.example.com[,https://…]
CSRF_TRUSTED_ORIGINS = _env_list("KB_CSRF_TRUSTED_ORIGINS", "")

# 运行期目录（vector_data 必须在项目根目录内）在导入配置时就创建并校验。
RUNTIME_DIRS = ensure_runtime_dirs()
MEDIA_ROOT = RUNTIME_DIRS["media"]
# 原始字节落盘根目录：真源是 config.paths.raw_store_dir()（默认 <项目根>/media/raw）
RAW_STORE_DIR = str(RUNTIME_DIRS["raw_store"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # governance 没有模型，注册它是为了让 governance/management/commands/ 下的
    # 管理命令（如 `manage.py guards`）能被 Django 发现。
    "governance",
    "apps.audit",
    "apps.knowledge",
    "apps.planner",
    # 浏览器验收界面（模板 + 静态资源；业务仍然走 REST API）
    "apps.webui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # 后端强制的不可篡改守卫，必须排在业务中间件之前生效（Django 自下而上执行，放最后即最先）
    "governance.middleware.ImmutabilityGuardMiddleware",
]

# 静态资源交付（容器/生产）：**不在 runserver 下也能提供静态文件**。
# 为什么用条件插入而不是直接写死：Windows 开发机上没有 whitenoise，
# 写死会让本机直接启动失败。装了才插，没装就不插（本机行为不变）。
try:  # pragma: no cover - 取决于部署环境是否安装
    import whitenoise  # noqa: F401
except ImportError:  # pragma: no cover
    pass
else:  # pragma: no cover
    # 紧跟在 SecurityMiddleware 之后，符合官方推荐顺序
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PROJECT_ROOT / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # 静态资源版本号：模板用它给 kb-*.js 加 ?v=，实现可靠缓存失效
                "apps.webui.context_processors.asset_version",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ------------------------------------------------------------------ 数据库 ----
DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite").strip().lower()
if DB_ENGINE in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _env_first("KB_PG_DB", "POSTGRES_DB", default="kb"),
            "USER": _env_first("KB_PG_USER", "POSTGRES_USER", default="kb"),
            "PASSWORD": _env_first("KB_PG_PASSWORD", "POSTGRES_PASSWORD", default=""),
            "HOST": _env_first("KB_PG_HOST", "POSTGRES_HOST", default="127.0.0.1"),
            "PORT": _env_first("KB_PG_PORT", "POSTGRES_PORT", default="5432"),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
            "OPTIONS": {
                "connect_timeout": int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "5")),
                # 关闭服务端游标缓存以外的隐式行为；连接级参数保持默认，避免和生产不一致
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(RUNTIME_DIRS["db"] / "kb.sqlite3"),
            "OPTIONS": {"timeout": 20},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 需求 3 要求"面向大并发"且不虚报能力：SQLite 根本撑不住并发写，只能在开发/测试用。
# 这里把口径写死：非 DEBUG 环境如果还连着 SQLite，直接拒绝启动，而不是"跑起来了但其实不可用"。
if not DEBUG and DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "生产配置禁止使用 SQLite（并发写会直接锁库）。请设置 DB_ENGINE=postgres "
        "并配置 KB_PG_HOST/KB_PG_PORT/KB_PG_DB/KB_PG_USER/KB_PG_PASSWORD。"
    )

# ---------------------------------------------------------------------- 认证 ----
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

# 浏览器界面的登录入口：用我们自己的 session 登录页（不是 Basic，也不是 Django admin）
LOGIN_URL = "/app/login/"
LOGIN_REDIRECT_URL = "/app/"
LOGOUT_REDIRECT_URL = "/app/login/"

# ---------------------------------------------------------------------- 时区 ----
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = PROJECT_ROOT / "staticfiles"

# ------------------------------------------------------------------ 缓存/队列 ----
REDIS_URL = _env_first("KB_REDIS_URL", "REDIS_URL", default="redis://127.0.0.1:6379/0")
USE_REDIS_CACHE = _env_bool("USE_REDIS_CACHE", False)
if USE_REDIS_CACHE:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    # 本地起步与单测不依赖 Redis（需求 3 要求不虚报能力，这里也不假装已有真实缓存）
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "kb-local",
        }
    }

# 向量库（Qdrant）：向量数据必须落在项目根目录内，见 config/paths.vector_data_dir()。
# P1 只登记 collection 的元数据（维度/模型/版本/激活态），真正写入向量是 P2。
QDRANT_URL = _env_first("KB_QDRANT_URL", "QDRANT_URL", default="http://127.0.0.1:6333")
QDRANT_API_KEY = _env_first("KB_QDRANT_API_KEY", "QDRANT_API_KEY", default="")
QDRANT_TIMEOUT_SECONDS = int(_env_first("KB_QDRANT_TIMEOUT", default="10"))
#: server（默认，生产口径）或 embedded（本地单进程，见 vectorindex 模块注释）
QDRANT_MODE = _env_first("KB_QDRANT_MODE", default="server")

# broker / result backend：优先读 **KB_** 前缀（与项目其它变量一致）。
#
# 为什么必须加这一层（部署审查发现的真实缺陷）：容器 env 文件里写的是
# ``KB_CELERY_BROKER_URL``，而这里原先只读 ``CELERY_BROKER_URL`` —— 于是容器
# 里这两个变量**根本没生效**，静默回落到业务 Redis（REDIS_URL）。那会让新
# worker 消费业务队列、碰到历史遗留消息，隔离形同虚设。
CELERY_BROKER_URL = _env_first(
    "KB_CELERY_BROKER_URL", "CELERY_BROKER_URL", default=REDIS_URL
)
CELERY_RESULT_BACKEND = _env_first(
    "KB_CELERY_RESULT_BACKEND", "CELERY_RESULT_BACKEND", default=REDIS_URL
)
# **默认 False（生产口径）**：eager 会让 apply_async 在**上传请求内**同步执行，
# 等于没有队列——远端模型服务掉线时请求会被长时间挂住（审查意见 7）。
# 本地验收想要同步，请显式 KB_UPLOAD_PROCESS_MODE=inline 或 CELERY_TASK_ALWAYS_EAGER=1。
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)

# beat 的真实调度：每 5 分钟扫一次"待处理/失败"的资料（看门狗式扫尾）。
# 只在 **beat 容器**里生效；测试与 web 进程不受影响（没人触发就不执行）。
# 可用 KB_CELERY_BEAT_SWEEP_SECONDS 调整间隔；设为 0 可关闭。
_BEAT_SWEEP = int(os.environ.get("KB_CELERY_BEAT_SWEEP_SECONDS", "300"))
if _BEAT_SWEEP > 0:
    CELERY_BEAT_SCHEDULE = {
        "sweep-pending-files": {
            "task": "knowledge.sweep_pending",
            "schedule": float(_BEAT_SWEEP),
            "kwargs": {"limit": 20, "retry_failed": True},
        },
        # **真实周期巡检**：只读检查"失败到上限/停滞/向量不完整/原件缺失"，
        # 返回可复盘的清单。间隔独立可调（KB_CELERY_BEAT_PATROL_SECONDS）。
        # 它与 sweep 的职责不同：sweep 是"推进待处理"，patrol 是"报告异常"。
        "patrol-integrity": {
            "task": "knowledge.patrol",
            "schedule": float(os.environ.get("KB_CELERY_BEAT_PATROL_SECONDS", "900")),
            "kwargs": {"limit": 200},
        },
    }
# 任务硬/软超时：必须**有界**。之前两项都没设，等于一个卡死的任务（比如外部模型
# 一直不返回）会永久占住 worker 的一个槽位，而且下面的 visibility_timeout 也失去意义
# （任务永远不返回时，任何"重新投递"都会同时跑两份）。
#   * 软超时给任务一次优雅失败的机会（可以写状态、留审计）；
#   * 硬超时到点由父进程杀掉子进程；配合 task_acks_on_failure_or_timeout=True，
#     超时的消息会被确认而不是无限重投（不会形成毒消息循环）。
CELERY_TASK_TIME_LIMIT = int(os.environ.get("KB_CELERY_TASK_TIME_LIMIT", "1800"))
# 软超时默认取硬超时的 4/5（1800 → 1440）。用比例而不是"硬超时减固定值"，
# 是因为运维把硬超时调小时（例如 120s）减法会算出非正数（测试 `test_env_overrides_are_honoured`
# 就是这么抓到这个 bug 的）。
CELERY_TASK_SOFT_TIME_LIMIT = int(
    os.environ.get(
        "KB_CELERY_TASK_SOFT_TIME_LIMIT", str(max(1, CELERY_TASK_TIME_LIMIT * 4 // 5))
    )
)

# broker 发布超时与重试：broker 掉线时必须快速失败，不能把请求堵住
#
# visibility_timeout（**崩溃恢复 SLA**，默认 3600s）：
# 实测结论（kombu 5.6.2 `transport/redis.py`：L369 写入的 score 是"变为未确认的时刻"，
# L415/419 用 `time() - visibility_timeout` 当下界做 zrevrangebyscore，L644 默认 3600）：
#   * worker 的**池子进程**被杀 → 父进程仍在，`task_reject_on_worker_lost` 立刻重投
#     （实测杀子进程后 40/40 在 3.5s 内成功）；
#   * **整个容器被 SIGKILL / 宿主宕机** → 父进程一起没了，没人 reject，只能等
#     visibility_timeout。实测默认值下，4 条在飞消息要再等 3554s（≈59 分钟）才回来，
#     期间重启的 worker 只能把队列里的 196 条做完，剩下 4 条一直 PENDING。
# 所以这里收紧成"任务硬超时 + 5 分钟余量"：崩溃后最长等这么久必然恢复，
# 又因为余量存在而不会把还活着的任务重复投递。
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "socket_timeout": float(os.environ.get("CELERY_BROKER_SOCKET_TIMEOUT", "2")),
    "socket_connect_timeout": float(os.environ.get("CELERY_BROKER_CONNECT_TIMEOUT", "2")),
    "retry_on_timeout": False,
    "max_retries": int(os.environ.get("CELERY_BROKER_MAX_RETRIES", "1")),
    "visibility_timeout": int(
        os.environ.get("KB_CELERY_VISIBILITY_TIMEOUT", str(CELERY_TASK_TIME_LIMIT + 300))
    ),
}

# 上传后的处理模式（见 config/celery.py 的说明）：
#   inline = 请求内同步处理（本地验收方便）
#   queue  = 落盘后立刻返回 202，由 worker 处理
#   auto   = 能派发就派发，否则明确返回"待处理"（默认）
# 远端模型服务掉线时，inline 会把请求挂住很久，这与"面向大并发"冲突，
# 所以生产建议 queue；本地默认 auto（eager 模式等价于同步，但状态口径统一）。
KB_UPLOAD_PROCESS_MODE = os.environ.get("KB_UPLOAD_PROCESS_MODE", "auto").strip().lower()

#: **离线/无模型模式**（UI 验收用）：AS711 未就绪时把它设为 1。
#:
#: 效果：所有对外模型调用（嵌入 / 重排 / OCR）**立刻**以"服务未启用"失败，
#: 不再尝试网络连接——于是界面不会卡几十秒等超时，也**绝不会**有任何真实外呼。
#: 代价说清楚：此时语义检索不可用，检索只剩关键字那一路（界面会如实标注），
#: 资料也无法建立向量索引（会停在失败/待处理，并给出明确原因）。
#:
#: 它与 ``KB_TEST_ADAPTER`` **不是一回事**：测试适配器会写假向量（所以只能写
#: 测试专用 collection）；离线开关**什么都不写**，因此不可能污染正式索引。
KB_MODEL_CALLS_DISABLED = _env_bool("KB_MODEL_CALLS_DISABLED", False)
#: 是否启用稠密（语义）检索。设为 0 时检索直接走关键字，不发任何嵌入请求。
KB_SEARCH_DENSE_ENABLED = _env_bool("KB_SEARCH_DENSE_ENABLED", True)

#: **跨进程模型外呼闸门**（Redis 排序集合信号量，实现见 ``apps/knowledge/ratelimit.py``）。
#:
#: 为什么需要：熔断器是进程内状态，多进程下"同时打给 AS711 的请求数"也是每个进程
#: 各算一份——web（检索/对话）与 worker（入库嵌入）同时开跑时会叠加，而 AS711 上四个
#: 模型服务跑在同一台机器上。所以额度必须放在进程都能看见的 Redis 里。
#:
#: 口径：Redis 不可用时**放行**（fail-open）并把降级次数计入指标 ``fallback``；
#: 拿不到额度时最多等 ``KB_MODEL_SLOT_WAIT_SECONDS``，超时按"服务不可用"如实上报。
KB_MODEL_LIMIT_ENABLED = _env_bool("KB_MODEL_LIMIT_ENABLED", True)
#: 每个服务允许的**同时在飞**外呼数（跨进程共享的总额度）。
KB_MODEL_MAX_CONCURRENCY = int(os.environ.get("KB_MODEL_MAX_CONCURRENCY", "4"))
#: 拿不到额度时最多等多久（秒）；等不到就如实报"服务不可用"，不放行。
KB_MODEL_SLOT_WAIT_SECONDS = float(os.environ.get("KB_MODEL_SLOT_WAIT_SECONDS", "10"))
#: 额度成员的存活时间（秒）：持有者被 SIGKILL 时靠它自愈。
#: **必须大于单次外呼最坏耗时**（当前最坏是 KB_LLM_DOC_TIMEOUT_SECONDS=180），
#: 否则正在飞的持有者会被误判为过期而超发。
KB_MODEL_SLOT_TTL_SECONDS = int(os.environ.get("KB_MODEL_SLOT_TTL_SECONDS", "300"))
#: 闸门用的 Redis 键前缀（与业务队列的键完全不重叠）。
KB_MODEL_LIMIT_PREFIX = os.environ.get("KB_MODEL_LIMIT_PREFIX", "kb:model:slots")
#: 闸门用的 Redis 地址；默认复用 Celery broker 的专用库（DB14），不碰业务库 DB0。
KB_MODEL_LIMIT_REDIS_URL = os.environ.get("KB_MODEL_LIMIT_REDIS_URL", "")

#: **负载测试环境标识**。只有显式设为 1 时，``/guide/`` 才会对外声明
#: ``load_test_mode=True``；压测脚本据此**fail-closed**：不是测试环境就拒绝发压。
#:
#: 为什么不复用 ``KB_MODEL_CALLS_DISABLED``：模型禁用只是一个必要条件，
#: 不代表性隔离（独立库/独立前缀/禁止业务派发）已经满足。
#: 两个条件都要满足，脚本才允许发压。
KB_LOAD_TEST_MODE = _env_bool("KB_LOAD_TEST_MODE", False)
#: 负载测试实例的**人工可读标识**（写进 /guide/，便于确认压的是哪一套）。
KB_LOAD_TEST_LABEL = os.environ.get("KB_LOAD_TEST_LABEL", "").strip()
#: 是否禁止把处理任务派发到队列。压测实例默认**禁止**，避免任务流进业务 broker。
KB_DISPATCH_DISABLED = _env_bool("KB_DISPATCH_DISABLED", False)


def _asset_version() -> str:
    """静态资源版本号：取 webui 静态目录里最新文件的修改时间。

    为什么需要它（实测踩到）：浏览器会长期缓存 ``/static/webui/kb-*.js``，
    改完脚本后用户刷新看到的还是旧代码——表现为"代码明明改了、界面没变"。
    模板给脚本 URL 带上 ``?v=<最新 mtime>`` 后，文件一变版本号就变，
    浏览器才会去取新脚本。取 mtime 比手工维护版本号更不容易忘。
    """
    static_dir = PROJECT_ROOT / "apps" / "webui" / "static" / "webui"
    try:
        latest = max(
            (_p.stat().st_mtime for _p in static_dir.iterdir() if _p.is_file()),
            default=0.0,
        )
    except OSError:
        latest = 0.0
    return f"{int(latest)}-{os.environ.get('KB_ASSET_VERSION', 'local')}"


KB_ASSET_VERSION = _asset_version()

# 处理租约（审查意见 7）：同一份资料不能被两条流水线同时处理。
# lease 过期后可以被接管，防止 worker 崩溃把资料永久卡死。
KB_PROCESS_LEASE_SECONDS = int(os.environ.get("KB_PROCESS_LEASE_SECONDS", "900"))

# DRF ------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "EXCEPTION_HANDLER": "config.exceptions.api_exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

# 治理与业务参数 ----------------------------------------------------------------
KB_ACTION_TICKET_TTL_SECONDS = int(os.environ.get("KB_ACTION_TICKET_TTL_SECONDS", "180"))
KB_DELETE_NAME_ATTEMPTS = int(os.environ.get("KB_DELETE_NAME_ATTEMPTS", "3"))
KB_UPLOAD_MAX_BYTES = int(os.environ.get("KB_UPLOAD_MAX_BYTES", str(200 * 1024 * 1024)))
KB_ALLOWED_UPLOAD_EXT = _env_list(
    "KB_ALLOWED_UPLOAD_EXT",
    # 图片格式不能漏：扫描件（.jpg/.png/…）是 P2 的 OCR 入口，解析器认、
    # 上传白名单也必须认，否则"支持扫描件"在接口层就被挡住了。
    ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.md,.txt,.csv,"
    ".png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff",
)

# 上传落盘的流式读写块大小
KB_UPLOAD_CHUNK_BYTES = int(os.environ.get("KB_UPLOAD_CHUNK_BYTES", str(1024 * 1024)))

# 检索与索引（P2）--------------------------------------------------------------
#: 每个库的每个 generation 对应一个 Qdrant collection，名字前缀可配置便于隔离环境
KB_QDRANT_COLLECTION_PREFIX = os.environ.get("KB_QDRANT_COLLECTION_PREFIX", "kb")
#: 单次嵌入请求最多几段（vLLM 侧有 max_num_seqs，不要超过）
KB_EMBED_BATCH_SIZE = int(os.environ.get("KB_EMBED_BATCH_SIZE", "16"))
#: 单段文本上限（字符），超长会在解析层先切分
KB_CHUNK_MAX_CHARS = int(os.environ.get("KB_CHUNK_MAX_CHARS", "1200"))
#: 混合检索：每路召回条数、最终返回条数
KB_SEARCH_DENSE_TOP_K = int(os.environ.get("KB_SEARCH_DENSE_TOP_K", "30"))
KB_SEARCH_SPARSE_TOP_K = int(os.environ.get("KB_SEARCH_SPARSE_TOP_K", "30"))
KB_SEARCH_RESULT_LIMIT = int(os.environ.get("KB_SEARCH_RESULT_LIMIT", "10"))
#: 是否启用 AS711 重排（关闭时只用 RRF 融合结果）
KB_SEARCH_USE_RERANK = _env_bool("KB_SEARCH_USE_RERANK", True)
#: 重排最多送入多少条候选
KB_RERANK_TOP_N = int(os.environ.get("KB_RERANK_TOP_N", "20"))
#: OCR 超时与单文件最多 OCR 页数（防止一本扫描书把服务打满）
KB_OCR_TIMEOUT_SECONDS = int(os.environ.get("KB_OCR_TIMEOUT_SECONDS", "120"))
KB_OCR_MAX_PAGES = int(os.environ.get("KB_OCR_MAX_PAGES", "200"))
KB_OCR_MAX_TOKENS = int(os.environ.get("KB_OCR_MAX_TOKENS", "4096"))
KB_OCR_REPETITION_PENALTY = float(os.environ.get("KB_OCR_REPETITION_PENALTY", "1.1"))
# OCR 在 web、worker 和人工恢复命令间共享一个额度，其他模型沿用原有并发。
KB_OCR_MAX_CONCURRENCY = int(os.environ.get("KB_OCR_MAX_CONCURRENCY", "2"))
KB_OCR_SLOT_WAIT_SECONDS = float(os.environ.get("KB_OCR_SLOT_WAIT_SECONDS", "300"))
#: 嵌入 / 重排超时。
#:
#: 为什么把**检索用**的超时压到 10 秒：模型服务断开时，一次检索要等
#: "超时 × 重试上限"才降级到关键字，界面上就是长时间"检索中"。
#: 实测默认 60 秒会让浏览器验收卡住一分多钟——而关键字那一路本可以立刻返回。
#: 嵌入是**短请求**（一句话几百 token），10 秒足够；文档批量入库另有
#: ``KB_EMBED_INDEX_TIMEOUT_SECONDS``（可以放宽，因为入库是后台任务）。
KB_EMBED_TIMEOUT_SECONDS = int(os.environ.get("KB_EMBED_TIMEOUT_SECONDS", "10"))
KB_EMBED_INDEX_TIMEOUT_SECONDS = int(
    os.environ.get("KB_EMBED_INDEX_TIMEOUT_SECONDS", "60")
)
KB_RERANK_TIMEOUT_SECONDS = int(os.environ.get("KB_RERANK_TIMEOUT_SECONDS", "15"))
#: 嵌入失败重试次数（只重试网络/5xx；维度不匹配一律不重试）
KB_EMBED_RETRIES = int(os.environ.get("KB_EMBED_RETRIES", "2"))

# 外呼模型服务的韧性（AS711 可能长时间不可用；见 apps/knowledge/resilience.py）
#: 连续失败多少次就熔断（之后快速失败，不再把请求打到挂掉的服务上）
KB_SERVICE_FAILURE_THRESHOLD = int(os.environ.get("KB_SERVICE_FAILURE_THRESHOLD", "3"))
#: 熔断冷却秒数；到点后进入半开，放一个请求试探
KB_SERVICE_COOLDOWN_SECONDS = int(os.environ.get("KB_SERVICE_COOLDOWN_SECONDS", "60"))

# 离线测试适配器 ---------------------------------------------------------------
# 只有显式打开时才启用"确定性假向量"，且**只允许写进测试专用 collection**，
# 不可能污染正式索引（见 apps/knowledge/testing_adapter.py 的硬校验）。
#: 取值：""（关闭，默认） / "offline"
KB_TEST_ADAPTER = os.environ.get("KB_TEST_ADAPTER", "").strip().lower()#: 测试向量维度：必须与 active profile 一致，避免"假维度"混进索引
KB_TEST_ADAPTER_DIMENSION = int(os.environ.get("KB_TEST_ADAPTER_DIMENSION", "1024"))

# 外部模型服务（AS711）。凭证一律走环境变量，仓库内不出现任何口令。
MODEL_API_KEY = _env_first("KB_MODEL_API_KEY", "MODEL_API_KEY", default="")
MODEL_SERVICES = {
    "llm": {
        "base_url": os.environ.get("AS711_LLM_BASE_URL", "http://172.16.100.114:10001/v1"),
        "model": os.environ.get("AS711_LLM_MODEL", "Qwen3.6-35B-A3B-FP8"),
    },
    "embedding": {
        "base_url": os.environ.get("AS711_EMBED_BASE_URL", "http://172.16.100.114:8000/v1"),
        "model": os.environ.get("AS711_EMBED_MODEL", "bge-large-zh-v1.5"),
        "dimension": int(os.environ.get("AS711_EMBED_DIM", "1024")),
    },
    "rerank": {
        "base_url": os.environ.get("AS711_RERANK_BASE_URL", "http://172.16.100.114:8002/v1"),
        "model": os.environ.get("AS711_RERANK_MODEL", "bge-reranker-large"),
    },
    "ocr": {
        "base_url": os.environ.get("AS711_OCR_BASE_URL", "http://172.16.100.114:8003/v1"),
        "model": os.environ.get("AS711_OCR_MODEL", "PaddleOCR-VL-1.5"),
        "max_num_seqs": int(os.environ.get("AS711_OCR_MAX_NUM_SEQS", "16")),
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"governance": {"handlers": ["console"], "level": "WARNING"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
}
