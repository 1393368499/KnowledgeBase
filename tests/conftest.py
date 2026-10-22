"""可复用的测试夹具。

**自动清理（⑦⑧）**
------------------
本会话会创建一个独占的 run（`run_uuid`），并把 `RAW_STORE_DIR` 指向
`artifacts/test-runs/<uuid>/raw` —— 测试上传的原件都落在这个**本次 run 专属**
目录里，会话结束自动清理（成功、断言失败、异常、Ctrl+C 都走 finally）。
清理失败会让 **pytest session 失败**，不会静默吞掉。
清理**只**按本次登记精确匹配，禁止 `kb-*` / `kb-lt-*` 模糊扫删；
历史遗留的 191 个集合（含 125 个 `kb-lt-*`、共约 42 GiB）**不在**自动删除范围内。


约定：
- 所有测试都在真实数据库约束下跑（不用 mock 掉 UniqueConstraint / 模型 save 守卫）；
- 权限用例统一用 ``make_user`` / ``make_library`` 构造，避免各测试各自造轮子；
- 时间相关用例统一用 ``freeze`` 之外的方式（直接改 ``expires_at``），减小脆弱性。

离线可跑（用户 2026-10-13 指示）
--------------------------------
默认由 ``config.test_settings`` 决定库：Postgres 连得上就用真 Postgres
（建独立测试库），连不上就退回 SQLite —— 所以没网、没容器时单测照样全绿。

- 想强制真 Postgres：``pytest --kb-db=postgres``（连不上会直接报错，不静默降级）
- 真实 Qdrant 用例：加 ``@pytest.mark.real_qdrant``，连不上自动 skip 并写明原因
- 真实 AS711 用例：加 ``@pytest.mark.real_as711``，默认 skip
"""

from __future__ import annotations

import itertools
import os
import socket

import pytest

# ---------------------------------------------------------------------------
# ⑦ 会话级 run：独占 run_uuid + 本次 run 专属 raw 目录
# ---------------------------------------------------------------------------
import os  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SESSION_RUN_UUID = os.environ.get("KB_TEST_RUN_UUID") or uuid.uuid4().hex[:12]
_SESSION_RUN_DIR = _PROJECT_ROOT / "artifacts" / "test-runs" / _SESSION_RUN_UUID
_SESSION_RAW_DIR = _SESSION_RUN_DIR / "raw"
_SESSION_VECTOR_DIR = _SESSION_RUN_DIR / "vector_data"

# **必须**在 Django settings 加载之前设置：config.paths.raw_store_dir() 是调用时
# 读环境变量的，晚设就只对部分用例生效。dotenv/settings 的加载时序不稳，
# 所以这里**无条件**把测试期间的 raw 指到本 run 隔离目录（不再"仅在未设置时才覆盖"）；
# 原值保存下来，会话结束再恢复，避免把隔离值留给后续进程。
_SESSION_RAW_DIR.mkdir(parents=True, exist_ok=True)
_ORIGINAL_RAW_STORE_DIR = os.environ.get("RAW_STORE_DIR")
os.environ["RAW_STORE_DIR"] = str(_SESSION_RAW_DIR)
_RAW_OVERRIDDEN = True

# 向量前缀默认是 "kb"（config/settings.py），会与**主实例历史集合**撞名
# （例如哨兵 kb-1-g1）。这里设成会话级唯一前缀，保证 pytest 只会创建/清理
# 属于自己的集合；原值同样保存并恢复。
_ORIGINAL_QDRANT_PREFIX = os.environ.get("KB_QDRANT_COLLECTION_PREFIX")
# 与 RunResources.prefix 保持一致（见 config/test_settings.py 里的说明）
_SESSION_QDRANT_PREFIX = f"kb-{_SESSION_RUN_UUID}"
os.environ["KB_QDRANT_COLLECTION_PREFIX"] = _SESSION_QDRANT_PREFIX

sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
import run_resources as _rr  # noqa: E402

_SESSION_RUN_RESOURCES = None
_SESSION_CLEANUP_ERROR: str = ""


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """⑧ 兜底：清理失败必须让会话退出码非零。"""
    if _SESSION_CLEANUP_ERROR:
        print(f"\n[自动清理失败] {_SESSION_CLEANUP_ERROR}", file=sys.stderr)
        session.exitstatus = 1


@pytest.fixture(scope="session", autouse=True)
def session_run_resources():
    """建本次 run → 登记本次 raw 目录 → 结束时自动清理（失败即 session 失败）。"""
    global _SESSION_RUN_RESOURCES, _SESSION_CLEANUP_ERROR
    endpoint = os.environ.get("KB_TEST_QDRANT_URL", "")
    res = _rr.RunResources.new(endpoint=endpoint, identity="pytest-session",
                               run_uuid=_SESSION_RUN_UUID)
    _SESSION_RUN_RESOURCES = res
    if _RAW_OVERRIDDEN:
        # 登记 → 创建确认：只有 created 的才会被清理
        res.declare("dir", str(_SESSION_RAW_DIR))
        res.confirm("dir", str(_SESSION_RAW_DIR))
        # 默认 embedded 的向量目录也在本 run 下，同样登记以便清理
        res.declare("dir", str(_SESSION_VECTOR_DIR))
        res.confirm("dir", str(_SESSION_VECTOR_DIR))
    try:
        yield res
    finally:
        try:
            # 打开前缀清点：覆盖默认上传/ensure_library_index 建出的真实集合
            res.cleanup(sweep_run_prefix=bool(endpoint))
        except Exception as exc:  # noqa: BLE001 - **不吞**：转成 session 失败
            _SESSION_CLEANUP_ERROR = f"{type(exc).__name__}: {exc}"
            res.release()
            _restore_test_env()
            raise
        res.release()
        _restore_test_env()


@pytest.fixture
def run_resources(session_run_resources):
    """给单个用例用：登记它自己创建的资源，交由同一套清理逻辑回收。"""
    return session_run_resources

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.knowledge.models import (
    IngestedFile,
    KnowledgeLibrary,
    LibraryLevel,
    LibraryMember,
    MemberRole,
    ParseStatus,
    VectorCollection,
)

User = get_user_model()


# --------------------------------------------------------------- 命令行开关 ----


def pytest_addoption(parser):
    group = parser.getgroup("kb")
    group.addoption(
        "--kb-db",
        action="store",
        default=None,     # 真正的默认在 pytest_load_initial_conftests 里定（见下）
        choices=["auto", "postgres", "sqlite"],
        help=(
            "测试库类型：sqlite（默认，离线确定）/ postgres（连不上就报错，"
            "用来验证真实 PostgreSQL）/ auto（探测）"
        ),
    )
    group.addoption(
        "--kb-qdrant",
        action="store_true",
        default=False,
        help="启用真实 Qdrant 用例（默认跳过）",
    )
    group.addoption(
        "--kb-broker",
        action="store_true",
        default=False,
        help="启用真实 Redis broker 往返用例（默认跳过）",
    )


def pytest_load_initial_conftests(early_config, parser, args):
    """**在 Django settings 加载之前**把 ``--kb-db`` 落到环境变量里。

    为什么必须这么早（实测踩到的真实缺陷）：``config/test_settings.py`` 在
    **pytest-django 的 configure 阶段**就被导入了，而 ``pytest_configure`` 里再
    ``os.environ.setdefault(...)`` 为时已晚 —— 于是 ``pytest --kb-db=postgres``
    会**静默地照旧跑 SQLite**，报告里却写着"已按开关强制 Postgres"。
    这是测试基础设施缺陷，不是产品缺陷；修法是提前到 initial conftest 阶段设置，
    并在下面用真实 ``connection.vendor`` 复核（对不上直接报错，不允许静默降级）。

    入口优先级：``KB_PYTEST_DB``（进程启动前设，.env 不会覆盖）>
    ``KB_TEST_DB`` > ``--kb-db`` > 默认 ``sqlite``。
    """
    for index, arg in enumerate(args):
        value = None
        if arg == "--kb-db" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--kb-db="):
            value = arg.split("=", 1)[1]
        if value:
            # 用 setdefault 语义：进程环境里已有的（KB_PYTEST_DB/KB_TEST_DB）优先，
            # 命令行只作为兜底入口，避免"命令行覆盖环境"造成另一种误判。
            os.environ.setdefault("KB_PYTEST_DB", value)
            break
    os.environ.setdefault("KB_PYTEST_DB", os.environ.get("KB_TEST_DB", "sqlite"))
    # 兼容老入口名（test_settings 也认它）
    os.environ.setdefault("KB_TEST_DB", os.environ["KB_PYTEST_DB"])


def pytest_configure(config):
    os.environ.setdefault("KB_TEST_DB", os.environ.get("KB_PYTEST_DB", "sqlite"))
    _verify_test_db_kind(config)


def _verify_test_db_kind(config) -> None:
    """复核"开关说的"和"真实连上的"是不是同一个库（不允许静默降级）。

    用真实 ``connection.vendor``：``postgresql`` / ``sqlite``。
    """
    try:
        from django.db import connection

        from config import test_settings
    except Exception:  # pragma: no cover - 环境未就绪时不阻塞收集
        return
    requested = str(
        getattr(test_settings, "KB_TEST_DB_KIND", "")
        or os.environ.get("KB_PYTEST_DB", "sqlite")
    ).lower()
    vendor = connection.vendor
    actual = "postgres" if vendor == "postgresql" else "sqlite"
    if requested in {"postgres", "postgresql"} and actual != "postgres":
        raise pytest.UsageError(
            "请求了 --kb-db=postgres / KB_PYTEST_DB=postgres，但真实连接是 "
            f"{vendor!r} —— 拒绝在 SQLite 上给出「已按 Postgres 验证」的结论。"
        )
    config._kb_db_actual = actual
    config._kb_db_vendor = vendor


def pytest_report_header(config):
    actual = getattr(config, "_kb_db_actual", None)
    vendor = getattr(config, "_kb_db_vendor", None)
    if actual:
        return [f"kb-test-db: {actual} (django connection.vendor={vendor!r})"]
    return []


# --------------------------------------------------------------- 基础设施探测 ----


def _reachable(host: str, port: int, *, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _split_host_port(url: str, default_host: str, default_port: int) -> tuple[str, int]:
    """从 ``scheme://host:port/path`` 取出 host/port（解析失败就退回默认）。

    为什么需要它：真实依赖的门控曾经**硬编码** 127.0.0.1:56333/56379，
    于是"在别的 Docker 网络里跑独占 PG/Redis/Qdrant"时会被错误地 skip
    （独立验收环境 `kb-audit-<run>` 就是这样）。现在显式环境变量优先。
    """
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(url if "//" in url else f"//{url}")
        host = parsed.hostname or default_host
        port = parsed.port or default_port
        return host, int(port)
    except Exception:  # noqa: BLE001 - 解析失败按默认值处理，不掩盖
        return default_host, default_port


def _qdrant_target() -> tuple[str, int, str]:
    """(host, port, url)：显式 ``KB_TEST_QDRANT_URL`` 优先，否则用 settings/默认端口。"""
    explicit = os.environ.get("KB_TEST_QDRANT_URL", "").strip()
    if explicit:
        host, port = _split_host_port(explicit, "127.0.0.1", 6333)
        return host, port, explicit.rstrip("/")
    host, port = _split_host_port(str(settings.QDRANT_URL), "127.0.0.1", 6333)
    return host, port, str(settings.QDRANT_URL).rstrip("/")


def _broker_target() -> tuple[str, int]:
    """(host, port)：显式 ``KB_TEST_BROKER_URL`` 优先，否则用本机发布端口。"""
    explicit = (
        os.environ.get("KB_TEST_BROKER_URL", "").strip()
        or os.environ.get("KB_TEST_REDIS_URL", "").strip()
    )
    if explicit:
        return _split_host_port(explicit, "127.0.0.1", 6379)
    return "127.0.0.1", 56379


@pytest.fixture(autouse=True)
def _no_broker_writes(monkeypatch):
    """**所有**测试都不向真实 broker 投递消息——包括 ``real_broker`` 用例。

    为什么必须这样：默认配置的 ``CELERY_BROKER_URL`` 指向业务 Redis，而
    ``KB_UPLOAD_PROCESS_MODE=auto`` 会真的派发——"跑一遍测试"就往业务队列
    塞一堆测试消息（实测累积 100+ 条 ``file_id=1`` 的垃圾）。

    **早期版本对 ``real_broker`` 直接跳过，那是错的**（审查意见）：真实 worker
    用例里的 ``_upload()`` 仍然调用真实上传接口，于是它会先经
    ``dispatch_processing`` 把消息投到**业务默认队列**——"专用 sender"只隔离了
    后面那次手工 ``send_task``。所以这里对**所有**用例无一例外地拦截默认派发；
    真实专项只能通过它自己的**专用 sender**投递。

    做法是替换**调用点**而不是改 broker 配置：Celery app 在测试配置生效前就
    建好了，事后改 conf 不能可靠重定向连接（实测只改 conf 时
    ``connection_for_write`` 仍解析到 127.0.0.1:6379/0）。
    """
    from config import celery as celery_module

    def fake_dispatch(file_id: int, *, queue: str = "", payload_extra=None) -> dict:
        return {
            "dispatched": False,
            "mode": "test",
            "task_id": "",
            "reason": "test_no_broker",
            "detail": "测试环境不向真实 broker 投递；真实派发请用 real_broker 用例的专用 sender",
        }

    monkeypatch.setattr(celery_module, "dispatch_processing", fake_dispatch)
    # 视图里是 from ... import dispatch_processing，模块属性也要一起换
    from apps.knowledge import views as knowledge_views

    if hasattr(knowledge_views, "dispatch_processing"):
        monkeypatch.setattr(knowledge_views, "dispatch_processing", fake_dispatch)
    yield


@pytest.fixture(autouse=True)
def _no_unmocked_external_calls(request, monkeypatch):
    """离线测试**禁止任何未 Mock 的 AS711 外呼**（审查方要求）。

    为什么需要它：AS711 断开时，一个"本该被 Mock 却漏了"的调用会去连真实内网，
    测试要么挂很久等超时、要么因为超时而失败，排查起来像是在测网络而不是测代码。

    做法：把"造 http 客户端"这个统一入口换成会立刻抛错的替身。
    需要真实网络的用例必须显式标 ``@pytest.mark.real_as711``；
    自己注入 ``client=MockTransport(...)`` 的用例不受影响（它们不走这个工厂）。
    """
    if request.node.get_closest_marker("real_as711") is not None:
        yield
        return
    from apps.knowledge import httpclient

    def forbidden(*args, **kwargs):  # pragma: no cover - 触发即测试失败
        raise AssertionError(
            "离线测试禁止真实外呼：请注入 client=httpx.Client(MockTransport(...))，"
            "或给用例加 @pytest.mark.real_as711（需要 AS711 在线）"
        )

    monkeypatch.setattr(httpclient, "build_http_client", forbidden)
    # embedding/rerank/ocr 在 import 时把函数绑到了自己模块的命名空间，
    # 所以这些模块里的名字也要一起换掉。
    from apps.knowledge import embedding, ocr, rerank

    for module in (embedding, rerank, ocr):
        if hasattr(module, "build_http_client"):
            monkeypatch.setattr(module, "build_http_client", forbidden)
    yield


@pytest.fixture(scope="session")
def qdrant_available(request) -> bool:
    """真实 Qdrant 是否可用（不发起业务请求，只测端口 + HTTP 探活）。

    端点来源：显式 ``KB_TEST_QDRANT_URL``（例如独占 Docker 网络里的
    ``http://qdrant:6333``）优先；否则用项目 settings 的 ``QDRANT_URL``。
    """
    if not request.config.getoption("--kb-qdrant") and not os.environ.get("KB_TEST_QDRANT"):
        return False
    host, port, url = _qdrant_target()
    if not _reachable(host, port):
        return False
    try:
        import httpx

        with httpx.Client(timeout=2, trust_env=False) as client:
            return client.get(f"{url}/collections").status_code == 200
    except Exception:  # noqa: BLE001
        return False


def pytest_collection_modifyitems(config, items):
    """把"需要真实依赖"的用例在没有依赖时明确 skip（不伪装成 pass）。"""
    skip_qdrant = pytest.mark.skip(
        reason="需要真实 Qdrant（docker compose up -d 后加 --kb-qdrant 启用；"
        "或用 KB_TEST_QDRANT_URL 指向独占实例）"
    )
    skip_as711 = pytest.mark.skip(
        reason="需要 AS711 模型服务（当前断开；恢复后用 --kb-as711 跑真实验证）"
    )
    qdrant_host, qdrant_port, _ = _qdrant_target()
    qdrant_ok = config.getoption("--kb-qdrant") and _reachable(qdrant_host, qdrant_port)
    as711_ok = bool(os.environ.get("KB_TEST_AS711"))
    broker_host, broker_port = _broker_target()
    broker_ok = bool(config.getoption("--kb-broker")) and _reachable(broker_host, broker_port)
    for item in items:
        if "real_qdrant" in item.keywords and not qdrant_ok:
            item.add_marker(skip_qdrant)
        if "real_as711" in item.keywords and not as711_ok:
            item.add_marker(skip_as711)
        if "real_broker" in item.keywords and not broker_ok:
            item.add_marker(
                pytest.mark.skip(
                    reason="需要真实 Redis broker（docker compose up -d 后加 --kb-broker 启用；"
                    "或用 KB_TEST_BROKER_URL 指向独占实例）"
                )
            )


@pytest.fixture(scope="session")
def test_db_kind() -> str:
    """当前实际用的测试库类型（报告里写清楚，避免"以为在测 Postgres"）。"""
    engine = settings.DATABASES["default"]["ENGINE"]
    return "postgres" if "postgresql" in engine else "sqlite"


# --------------------------------------------------------------- 通用夹具 ----


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def make_user(db):
    def _make(username: str = "alice", *, staff: bool = False, password: str = "pw-not-a-secret-123"):
        return User.objects.create_user(
            username=username, password=password, is_staff=staff, is_superuser=staff
        )

    return _make


@pytest.fixture
def admin_user(make_user):
    return make_user("admin", staff=True)


@pytest.fixture
def user(make_user):
    return make_user("alice")


@pytest.fixture
def other_user(make_user):
    return make_user("bob")


@pytest.fixture
def collection(db) -> VectorCollection:
    return VectorCollection.objects.create(
        name="kb_default",
        dimension=1024,
        embedding_model="bge-large-zh-v1.5",
        storage_mode="server",
        storage_path="vector_data/qdrant",
    )


@pytest.fixture
def make_library(db, collection):
    def _make(
        code: str = "product",
        name: str = "产品信息库",
        *,
        level: str = LibraryLevel.INTERNAL,
        customer: str = "",
        department: str = "产品部",
        active: bool = True,
    ) -> KnowledgeLibrary:
        return KnowledgeLibrary.objects.create(
            code=code,
            name=name,
            level=level,
            customer=customer,
            department=department,
            is_active=active,
            vector_collection=collection,
        )

    return _make


@pytest.fixture
def library(make_library) -> KnowledgeLibrary:
    return make_library()


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """把项目根、向量目录与原始字节目录都指到临时目录（离线集成用例共用）。

    为什么要连 ``PROJECT_ROOT`` 一起换：``storage`` / ``vectorindex`` 会对落盘路径
    做"必须在项目根内"的硬校验（需求 2）。测试要验证真实落盘，就得把项目根也
    指到临时目录，否则只能把测试数据写进真实 ``media/raw``——那是污染，不是测试。
    """
    from pathlib import Path

    from django.test import override_settings

    fake_root = tmp_path / "kb-root"
    vector_dir = fake_root / "vector_data"
    raw_dir = fake_root / "media" / "raw"
    vector_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    import config.paths as paths

    monkeypatch.setattr(paths, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(paths, "vector_data_dir", lambda: vector_dir)
    monkeypatch.setattr(paths, "qdrant_server_dir", lambda: vector_dir / "qdrant")
    monkeypatch.setattr(paths, "qdrant_embedded_dir", lambda: vector_dir / "embedded")
    monkeypatch.setattr(paths, "raw_store_dir", lambda: raw_dir)
    assert Path(fake_root).is_dir()

    with override_settings(
        QDRANT_MODE="embedded",
        KB_TEST_ADAPTER="offline",
        KB_TEST_ADAPTER_DIMENSION=1024,
    ):
        yield {"root": fake_root, "vector": vector_dir, "raw": raw_dir}


@pytest.fixture
def offline_paths(tmp_path, monkeypatch):
    """离线模式（无模型）用例专用：隔离路径，但**不打开测试适配器**。

    为什么不能复用 ``isolated_paths``：那个夹具会打开 ``KB_TEST_ADAPTER=offline``，
    于是流水线会拿到**确定性假向量**并且"成功"——恰好掩盖了离线模式要验证的东西
    （离线模式必须**什么都不写**，而不是写假向量）。这里刻意把适配器关掉。
    """
    from pathlib import Path

    from django.test import override_settings

    fake_root = tmp_path / "kb-offline-root"
    vector_dir = fake_root / "vector_data"
    raw_dir = fake_root / "media" / "raw"
    vector_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    import config.paths as paths

    monkeypatch.setattr(paths, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(paths, "vector_data_dir", lambda: vector_dir)
    monkeypatch.setattr(paths, "qdrant_server_dir", lambda: vector_dir / "qdrant")
    monkeypatch.setattr(paths, "qdrant_embedded_dir", lambda: vector_dir / "embedded")
    monkeypatch.setattr(paths, "raw_store_dir", lambda: raw_dir)
    assert Path(fake_root).is_dir()

    with override_settings(
        QDRANT_MODE="embedded",
        KB_TEST_ADAPTER="",
        KB_MODEL_CALLS_DISABLED=True,
    ):
        yield {"root": fake_root, "vector": vector_dir, "raw": raw_dir}


@pytest.fixture
def library_setup_vector(library, isolated_paths):
    """给"上传/队列"用例准备一个库：**不预造 collection**。

    上传接口现在会自动初建索引（见 ``pipeline.ensure_library_index``），
    所以用例应该像真实用户那样"建库 → 上传"，而不是先手工把 collection 造好。
    这里只把向量库客户端注入流水线（本地嵌入模式不能开第二个客户端）。
    """
    from apps.knowledge import pipeline as pipeline_module
    from apps.knowledge import vectorindex as vi

    client = vi.build_client()
    original = pipeline_module.QDRANT_CLIENT_FACTORY
    pipeline_module.QDRANT_CLIENT_FACTORY = lambda: client
    try:
        yield client
    finally:
        pipeline_module.QDRANT_CLIENT_FACTORY = original
        client.close()


@pytest.fixture
def qdrant_client(isolated_paths):
    """真实 qdrant-client（本地嵌入模式，落在临时目录）。"""
    from apps.knowledge import vectorindex

    client = vectorindex.build_client()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def grant_member(db):
    def _grant(library: KnowledgeLibrary, user, role: str = MemberRole.WRITER) -> LibraryMember:
        return LibraryMember.objects.create(library=library, user=user, role=role)

    return _grant


@pytest.fixture
def auth_api(api):
    def _auth(user):
        api.force_authenticate(user=user)
        return api

    return _auth


@pytest.fixture
def make_file(db):
    counter = itertools.count()

    def _make(
        library: KnowledgeLibrary,
        name: str = "合同.md",
        *,
        sha: str = "",
        status: str = ParseStatus.DONE,
        source: str = "manual",
        directory_path: str = "产品信息库/技术接口",
        vector_collection: str | None = None,
        **extra,
    ) -> IngestedFile:
        # 同名多次调用必须产生不同指纹，否则会撞 (library, content_sha256) 唯一约束
        sha = sha or f"{abs(hash((library.pk, name, next(counter)))) :064x}"[:64]
        # vector_collection 显式声明为形参：否则调用方一旦也传它，
        # 就会和下面那个默认值组成"重复关键字参数"直接 TypeError
        # （审查方实测到 13 条用例因此失败）。
        return IngestedFile.objects.create(
            library=library,
            original_name=name,
            content_sha256=sha,
            size_bytes=extra.pop("size_bytes", 1024),
            file_type=name.rsplit(".", 1)[-1].lower(),
            directory_path=directory_path,
            parse_status=status,
            source=source,
            vector_collection=vector_collection
            if vector_collection is not None
            else library.vector_collection,
            **extra,
        )

    return _make

# ---------------------------------------------------------------------------
# 存储迁移测试用的隔离夹具源（跨文件共享）
# ---------------------------------------------------------------------------
import shutil as _shutil  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


@pytest.fixture
def fixture_source(tmp_path):
    """项目 ``artifacts/`` 下、名字含 fixture 的隔离夹具源。

    必须落在 artifacts 下且名字含 fixture —— 这是**夹具模式的技术限制**
    （``storage_migrate.assert_fixture_source``），放系统临时目录会被拒绝。
    用完即删（只删自己刚建的那个目录）。
    """
    root = _Path(__file__).resolve().parents[1]
    # 会话唯一 + 每次唯一（以前是全局同名 pytest-fixture-<test_name>）
    import uuid as _uuid2

    base = (root / "artifacts" / "test-runs" / _SESSION_RUN_UUID
            / "fixtures" / f"source-{_uuid2.uuid4().hex[:8]}")
    src = base / "src"
    (src / "collections" / "kb-1-g1").mkdir(parents=True, exist_ok=True)
    (src / "collections" / "kb-1-g1" / "a.bin").write_bytes(b"x" * 2048)
    (src / "meta.json").write_text('{"v":1}', encoding="utf-8")
    yield src, base / "backup"
    # **不用 ignore_errors**：夹具清理失败必须可见（否则会留下"看不见的残留"）。
    # 且先做真实绝对包含校验：只允许删本 session 的 fixtures 根之下。
    allowed = _Path(os.path.realpath(root / "artifacts" / "test-runs"
                                     / _SESSION_RUN_UUID / "fixtures"))
    real = _Path(os.path.realpath(base))
    try:
        inside = os.path.commonpath([str(real), str(allowed)]) == str(allowed)
    except ValueError:
        inside = False
    if not inside or real == allowed:
        raise AssertionError(f"拒绝删除越界路径：{real}（允许根 {allowed}）")
    _shutil.rmtree(real)


def _restore_test_env() -> None:
    """恢复被我们改过的环境变量（不把测试期的隔离值留给后续进程）。"""
    if _ORIGINAL_RAW_STORE_DIR is None:
        os.environ.pop("RAW_STORE_DIR", None)
    else:
        os.environ["RAW_STORE_DIR"] = _ORIGINAL_RAW_STORE_DIR
    if _ORIGINAL_QDRANT_PREFIX is None:
        os.environ.pop("KB_QDRANT_COLLECTION_PREFIX", None)
    else:
        os.environ["KB_QDRANT_COLLECTION_PREFIX"] = _ORIGINAL_QDRANT_PREFIX

@pytest.fixture
def isolated_namespace(run_resources):
    """**本 run 内**的向量命名空间 + 模型禁用 + 非 eager + 禁止派发（跨文件共享）。

    历史上这里是 ``kb-lt-<随机>`` 且**完全没有清理** —— 它是 42 GiB 里
    125 个 ``kb-lt-*`` 集合的主要来源之一：随机前缀不属于任何 run，
    清理器按设计拒绝碰它，于是永久残留。

    现在改成 ``kb-<session_uuid>-c<短随机>``：
      * 以会话 run 独占前缀开头 → 会被「本 run 前缀清点」回收
        （该清点**先审计后删除**，只匹配本 run 前缀，绝不是扫 kb-*）；
      * 短随机后缀避免同会话内并发用例撞名。
    """
    import uuid as _uuid

    from django.test import override_settings

    session_uuid = os.environ.get("KB_TEST_RUN_UUID", "")
    if not session_uuid:
        pytest.fail("缺少 KB_TEST_RUN_UUID —— 会话隔离未生效，拒绝用随机前缀建集合")
    namespace = f"kb-{session_uuid}-c{_uuid.uuid4().hex[:6]}"
    with override_settings(
        KB_QDRANT_COLLECTION_PREFIX=namespace,
        KB_MODEL_CALLS_DISABLED=True,
        KB_UPLOAD_PROCESS_MODE="queue",
        CELERY_TASK_ALWAYS_EAGER=False,
        KB_DISPATCH_DISABLED=True,
    ):
        yield namespace


# --------------------------------------- 自动释放测试里创建的所有 run 锁 ----
@pytest.fixture(autouse=True)
def _auto_release_runs(monkeypatch):
    """把本用例创建的所有 `RunResources` owner 在 teardown **无条件 release**。

    为什么需要：Windows 上未释放的 `run.lock` 句柄会让夹具的 rmtree 报
    `WinError 32`。以前用"有界重试"掩盖它，那等于把"忘记释放"藏起来。
    fixture finalizer 在**断言失败时同样执行**，语义等价于 finally。
    """
    import sys as _sys
    from pathlib import Path as _P

    _sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "scripts"))
    import run_resources as _rr

    created: list = []
    original = _rr.RunResources.new.__func__

    def tracking(cls, **kwargs):
        obj = original(cls, **kwargs)
        created.append(obj)
        return obj

    monkeypatch.setattr(_rr.RunResources, "new", classmethod(tracking))
    yield
    for obj in created:
        try:
            obj.release()
        except Exception:  # noqa: BLE001 - 释放失败不该掩盖真实断言
            pass
