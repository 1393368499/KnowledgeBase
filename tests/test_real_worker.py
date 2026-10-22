"""真实 Celery worker 往返：真的起 worker 进程消费，真的核对副作用。

关键事实（已用短验证确认）
--------------------------
PostgreSQL 的测试库是**服务器上的真实数据库**：子进程只要连**同一个 NAME**
就能看到父进程已提交的数据（``django_db(transaction=True)`` 会真的提交）。
所以本模块不需要、也不允许切业务库或删业务记录。

三重隔离（每一重都必须有）
--------------------------
1. **数据库**：父进程跑在 ``--kb-db=postgres`` 的测试库上；子进程用
   ``config.worker_test_settings``，库名由
   ``KB_WORKER_DB_NAME = connection.settings_dict["NAME"]`` 传入
   （Django 实际在用的那个），凭据仍来自 ``.env``（.env 不提供库名）。
2. **broker**：固定测试专用 Redis DB（``KB_TEST_BROKER_DB``，默认 15）+
   **每用例随机队列** ``kb-test-<uuid>``。只删自己的队列与自己的结果键，
   **绝不 purge 业务队列**。
3. **向量集合命名空间**：``KB_QDRANT_COLLECTION_PREFIX`` 设成
   **父子进程共享的随机前缀**（``kb-t-<uuid>``）。本次测试建的 collection
   都落在自己的命名空间里，**不可能**与业务 collection 名碰撞；结束时按前缀
   只删**自己**建的那些（autouse 夹具，异常路径也清）。

发送端刻意不复用生产 Celery app：生产 app 按业务 settings 建好，反复
``close()`` / ``config_from_object()`` 既不稳也会污染同进程其它用例。
这里用**一个全新的 Celery 实例**发送与取结果。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from celery import Celery
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, ParseStatus

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.real_broker]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# **按实际运行平台选解释器**：以前优先用 `.venv/Scripts/python.exe`，
# 那是 Windows 布局；在 Linux 容器里如果这份 checkout 恰好也带着宿主
# 的 .venv（RO 绑定挂载就是这种情况），就会选中一个 Linux 跑不了的
# Windows exe（ExecFormatError）。跑 pytest 的解释器一定与当前平台匹配。
PYTHON = sys.executable

TASK_NAME = "knowledge.process_ingested_file"
TEST_BROKER_DB = os.environ.get("KB_TEST_BROKER_DB", "15")
WORKER_TIMEOUT = float(os.environ.get("KB_TEST_WORKER_TIMEOUT", "60"))


# ------------------------------------------------------------------ 环境 ----


def _redis_host_port() -> tuple[str, str]:
    raw = os.environ.get("KB_REDIS_URL", "redis://127.0.0.1:56379/1")
    host_port = raw.split("//", 1)[-1].split("/", 1)[0]
    host, _, port = host_port.partition(":")
    return host or "127.0.0.1", port or "6379"


def _broker_url() -> str:
    host, port = _redis_host_port()
    return f"redis://{host}:{port}/{TEST_BROKER_DB}"


def _redis_client():
    import redis

    host, port = _redis_host_port()
    return redis.Redis(host=host, port=int(port), db=int(TEST_BROKER_DB), socket_timeout=3)


def _test_db_name() -> str:
    """Django **实际在用**的测试库名（服务器上真实存在的那个）。"""
    from django.db import connection

    return str(connection.settings_dict["NAME"])


# ------------------------------------------------------------------ 夹具 ----


@pytest.fixture
def namespace() -> str:
    """父子进程共享的集合命名空间，**落在本 run 独占前缀内**。

    以前是 ``kb-t-<随机>``：它不属于任何 run，因此会话清理的「本 run 前缀清点」
    **回收不到**它，只能靠下面那次清理；而那次清理又把异常吞掉了 →
    一旦失败就永久残留（与 42 GiB 历史问题同源）。
    现在以 ``kb-<session_uuid>-`` 开头，双保险：即使 _drop_test_collections 失败，
    会话清理仍能按前缀把它收走。
    """
    session_uuid = os.environ.get("KB_TEST_RUN_UUID", "")
    if not session_uuid:
        pytest.fail("缺少 KB_TEST_RUN_UUID —— 会话隔离未生效，拒绝用 run 外前缀建集合")
    return f"kb-{session_uuid}-w{uuid.uuid4().hex[:6]}"


@pytest.fixture
def queue_name() -> str:
    return f"kb-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def sender(queue_name, monkeypatch):
    """发送端：全新 Celery 实例 + 独立 Redis DB（不复用生产 app）。

    **为什么要同时改环境变量**：Celery 5 会读 ``CELERY_*`` 环境变量，优先级
    **高于** ``app.conf.update(...)``；而项目 ``.env``（``load_dotenv(override=True)``）
    把 ``CELERY_BROKER_URL`` 设成了业务 ``.../0``，于是新实例无论怎么 update，
    ``conf.broker_url`` 读回来仍是 ``/0``（实测确认）。所以环境变量也要对齐
    （monkeypatch 结束自动还原）。
    """
    url = _broker_url()
    monkeypatch.setenv("CELERY_BROKER_URL", url)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", url)
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "0")

    instance = Celery("kb-test-sender")
    instance.conf.update(
        task_always_eager=False,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        broker_transport_options={
            "socket_timeout": 3,
            "socket_connect_timeout": 3,
            "retry_on_timeout": False,
        },
    )
    instance.conf.update(broker_url=url, result_backend=url)
    uri = instance.connection_for_write().as_uri()
    assert uri == url, f"sender 的 broker 不是独立 DB：{uri} ≠ {url}"

    yield instance

    try:
        instance.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def cleanup(queue_name):
    """只清理**本次测试自己**产生的结果键。绝不碰业务队列。"""
    created_task_ids: list[str] = []

    def track(task_id: str) -> None:
        if task_id:
            created_task_ids.append(task_id)

    yield {"track": track}

    client = _redis_client()
    try:
        client.delete(queue_name)
        # kombu 会在 worker 启动时声明队列，留下 binding 键；它也是"本次测试自己
        # 产生的"，一并删掉，避免测试库越积越脏（**只删自己队列的 binding**）。
        client.delete(f"_kombu.binding.{queue_name}")
        for task_id in created_task_ids:
            client.delete(f"celery-task-meta-{task_id}")
    finally:
        client.close()


@pytest.fixture
def created_library(db):
    """本用例的库：随机 code，避免与其它运行冲突。"""
    code = f"worker-test-{uuid.uuid4().hex[:8]}"
    return KnowledgeLibrary.objects.create(code=code, name=f"worker 测试库 {code}")


def _list_collections(client) -> list[str]:
    """列出 Qdrant 里的 collection 名（qdrant-client 的返回结构随版本有差异）。"""
    result = client.get_collections()
    items = getattr(result, "collections", None)
    if items is None and isinstance(result, dict):
        items = result.get("collections", [])
    names: list[str] = []
    for item in items or []:
        name = getattr(item, "name", None)
        if name is None and isinstance(item, dict):
            name = item.get("name")
        if name:
            names.append(str(name))
    return names


def _drop_test_collections(namespace: str) -> list[str]:
    """删掉本命名空间下的 collection（**只删自己的**）。"""
    from apps.knowledge import vectorindex

    client = vectorindex.build_client()
    dropped: list[str] = []
    try:
        for name in _list_collections(client):
            if name.startswith(namespace):
                client.delete_collection(name)
                dropped.append(name)
    finally:
        client.close()
    return dropped


@pytest.fixture(autouse=True)
def _parent_namespace(namespace, run_resources):
    """**父进程也必须在同一个 collection 命名空间里**（审查指出的缺口）。

    早期版本只把 ``namespace`` 传给子进程和用于断言，父进程并没有应用它——
    于是测试进程自己调用上传接口时，会先在**正式命名空间**建出
    ``kb-<库id>-g1``（`_upload` 触发 `ensure_library_index`），
    然后才在断言里发现"不在测试命名空间"。那既污染正式 Qdrant，也让断言失败。

    所以这里用 autouse + ``override_settings`` 在任何用例代码（含 upload）之前
    就把父进程的 ``KB_QDRANT_COLLECTION_PREFIX`` 与 ``QDRANT_MODE`` 定好；
    teardown 的清理**在覆盖仍然有效时**执行。
    """
    with override_settings(KB_QDRANT_COLLECTION_PREFIX=namespace, QDRANT_MODE="server"):
        yield
        # 清理必须发生在覆盖生效期间（此时前缀仍是本用例的命名空间）。
        # worker 子进程的 fixture 在**本 autouse 之前**就完成 teardown
        # （finalizer 逆序），所以到这里 worker 已经退出、不会再写新集合。
        #
        # **不吞异常**：清理失败必须可见（用户要求"清理失败非零、不可 ignore_errors"）。
        # 早先这里是 `except Exception: pass` —— 一旦失败就永久残留，
        # 而那正是 42 GiB 历史问题的成因之一。
        # **先登记审计，再删除**：把本命名空间下的集合 declare+confirm 进
        # 本 session 的 run，这样删除动作进入统一审计（manifest/cleanup-report），
        # 而不是"绕过登记偷偷删"。会话清理结尾仍会按本 run 前缀兜底清点，
        # 已删的条目按幂等处理（不存在即视为成功）。
        from apps.knowledge import vectorindex

        client = vectorindex.build_client()
        try:
            names = [n for n in _list_collections(client) if n.startswith(namespace)]
        finally:
            client.close()
        for name in names:
            run_resources.declare("qdrant_collection", name)
            run_resources.confirm("qdrant_collection", name)

        dropped = _drop_test_collections(namespace)

        # 复核（`_list_collections` 签名是 `(client)`）：
        client = vectorindex.build_client()
        try:
            remaining = [n for n in _list_collections(client) if n.startswith(namespace)]
        finally:
            client.close()
        if remaining:
            raise AssertionError(
                f"清理不彻底：命名空间 {namespace} 仍有集合 {remaining}"
                f"（已删 {dropped}）—— 清理失败必须显式失败，不得静默吞掉"
            )


# ------------------------------------------------------------ 本地嵌入桩 ----


class _EmbeddingStub:
    """本地隔离的假嵌入服务（本机随机端口），让 worker 走**真实 HTTP 路径**。

    只监听回环地址，绝不外呼 AS711。向量由文本哈希稳定生成（同文本同向量），
    且只会写进**本测试命名空间**的 collection。
    """

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.requests = 0
        self.port = 0
        self._server = None
        self._vectors: dict[str, list[float]] = {}

    def _vector_for(self, text: str) -> list[float]:
        if text not in self._vectors:
            import hashlib

            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
            self._vectors[text] = [
                ((seed >> (i % 64)) & 0xFF) / 255.0 for i in range(self.dimension)
            ]
        return self._vectors[text]

    def __enter__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                stub.requests += 1
                body = json.dumps(
                    {
                        "data": [
                            {"index": i, "embedding": stub._vector_for(text)}
                            for i, text in enumerate(payload.get("input") or [])
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        return False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


@pytest.fixture
def embedding_stub():
    with _EmbeddingStub() as stub:
        yield stub


# --------------------------------------------------------------- worker ----


def _start_worker(queue_name: str, namespace: str, *, embedding_url: str) -> subprocess.Popen:
    """起真实 worker：同一测试库 + 独立 broker DB + **同一 collection 命名空间**。

    传给子进程的是 ``config.worker_test_settings`` 要求的**显式覆盖项**
    （缺任何一项该模块会 fail-closed 拒绝启动）。**连接凭据也由这里显式下传**：
    子进程绝不回读生产 ``.env`` 的 host/port/user/password —— 独立审查发现的真实失败
    正是"父进程传了独占 ``pg:5432``，子进程却被 ``.env`` 的 ``127.0.0.1:55432`` 盖掉"，
    任务全部因 PG 拒绝而失败。密码允许为空（trust/peer 认证），但**键必须显式存在**。
    """
    env = dict(os.environ)
    # **端点与目录必须显式传给子进程**（真实复验发现的缺陷）：
    # 只传 prefix 时，子 worker 会经 .env 拿到**主实例 56333** 并在那里写，
    # 而父进程的 finally 在隔离端点 56339 清 → 泄漏 + 误碰主实例。
    from django.conf import settings as _dj_settings
    from django.db import connection as _connection

    # 父连接的事实来源 = Django 当前实际在用的那条连接（也就是当前测试库）
    _db = _connection.settings_dict

    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.worker_test_settings",
            "KB_WORKER_DB_NAME": _test_db_name(),
            "KB_WORKER_BROKER_URL": _broker_url(),
            "KB_WORKER_QDRANT_PREFIX": namespace,
            "KB_WORKER_EMBED_BASE_URL": embedding_url,
            # ① 隔离端点（与父进程**同一个**）；worker_test_settings 会校验它不是主实例
            "KB_WORKER_QDRANT_URL": str(_dj_settings.QDRANT_URL),
            # ② 隔离 raw / vector 目录（都在本 run 下，结束时随会话清理）
            "KB_WORKER_RAW_STORE_DIR": str(_dj_settings.RAW_STORE_DIR),
            "KB_WORKER_VECTOR_DATA_DIR": str(_dj_settings.VECTOR_DATA_DIR),
            # ③ **连接凭据显式下传**（空密码也照样传，绝不回落 .env）
            "KB_PG_HOST": str(_db.get("HOST") or ""),
            "KB_PG_PORT": str(_db.get("PORT") or ""),
            "KB_PG_USER": str(_db.get("USER") or ""),
            "KB_PG_PASSWORD": str(_db.get("PASSWORD") or ""),
        }
    )
    return subprocess.Popen(
        [
            PYTHON,
            "-m",
            "celery",
            "-A",
            "config",
            "worker",
            "--queues",
            queue_name,
            "--concurrency",
            "1",
            "--pool",
            "solo",
            "--loglevel",
            "INFO",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _collect_worker_log(process: subprocess.Popen, timeout: float = 45.0) -> list[str]:
    """等 worker 真正就绪。

    判据必须是 ``" ready."``（Celery 打印 ``celery@host ready.``）——
    ``celery@`` 横幅在**连接成功之前**就输出了，用它当判据会在 worker
    还没订阅队列时就开始投递，产生假阴性。
    """
    import threading

    lines: list[str] = []
    lock = threading.Lock()

    def reader(stream):
        if stream is None:
            return
        for line in stream:
            with lock:
                lines.append(line.rstrip())

    threading.Thread(target=reader, args=(process.stdout,), daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            joined = "\n".join(lines)
        if " ready." in joined:
            return list(lines)
        if process.poll() is not None:
            break
        time.sleep(0.3)
    with lock:
        return list(lines)


def _stop_worker(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


def _wait_db_state(file_id: int, *, expect=ParseStatus.DONE, timeout: float = WORKER_TIMEOUT):
    """等数据库状态真的变成 ``expect``（跨进程可信判据）。"""
    from django.db import connections

    deadline = time.time() + timeout
    record = None
    while time.time() < deadline:
        connections.close_all()  # 丢掉连接快照，读 worker 提交后的最新值
        record = IngestedFile._base_manager.filter(pk=file_id).first()
        if record is not None and record.parse_status == expect:
            return record
        time.sleep(0.5)
    return record


def _upload(api, user, library, filename: str, body: bytes) -> IngestedFile:
    api.force_authenticate(user=user)
    response = api.post(
        "/api/v1/knowledge/files/upload/",
        {"library": library.code, "file": SimpleUploadedFile(filename, body)},
        format="multipart",
    )
    assert response.status_code in {200, 201, 202}, response.content[:400]
    return IngestedFile._base_manager.get(pk=response.json()["file"]["id"])


def _dispatch(sender, queue_name: str, record: IngestedFile, library: KnowledgeLibrary):
    """用新 sender 投递，返回同一个 sender 上的 AsyncResult。"""
    return sender.send_task(
        TASK_NAME,
        args=[record.pk],
        kwargs={
            "content_sha256": record.content_sha256,
            "library_code": library.code,
        },
        queue=queue_name,
    )


def _assert_state_success(result, log: list[str]) -> dict:
    """严格核对：**同一个 task_id** 达到 SUCCESS，且返回 ok=True。

    只看 payload 不够（无法区分"任务成功"与"结果没落库"）；这里显式断言
    Celery 侧状态为 SUCCESS。
    """
    payload = result.get(timeout=WORKER_TIMEOUT, propagate=False)
    assert result.state == "SUCCESS", (
        f"任务状态不是 SUCCESS：{result.state}；结果={payload}\n" + "\n".join(log[-30:])
    )
    assert payload is not None, "任务没有返回结果：\n" + "\n".join(log[-30:])
    assert payload.get("ok") is True, f"任务返回失败：{payload}\n" + "\n".join(log[-30:])
    return payload


# ---------------------------------------------------------------- 用例 ----


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", CELERY_TASK_ALWAYS_EAGER=False)
def test_real_worker_roundtrip_consumes_and_indexes(
    api, admin_user, created_library, namespace, queue_name, sender, cleanup, embedding_stub
):
    """真实 worker 消费 → 同 task SUCCESS + ok=True → DB done + 本命名空间有点。"""
    from apps.knowledge import vectorindex

    record = _upload(api, admin_user, created_library, "worker.txt", "质保期 12 个月".encode())
    created_library.refresh_from_db()
    collection = created_library.vector_collection
    assert collection, "上传接口应自动初建索引"
    # 父进程自己建的 collection 也必须落在测试命名空间里
    assert collection.startswith(namespace), f"collection 不在测试命名空间：{collection}"
    assert record.parse_status == ParseStatus.PENDING
    worker = _start_worker(queue_name, namespace, embedding_url=embedding_stub.base_url)
    try:
        log = _collect_worker_log(worker)
        assert " ready." in "\n".join(log), "worker 没就绪：\n" + "\n".join(log[-30:])
        result = _dispatch(sender, queue_name, record, created_library)
        cleanup["track"](result.id)
        payload = _assert_state_success(result, log)
        assert payload["file_id"] == record.pk
        done = _wait_db_state(record.pk, expect=ParseStatus.DONE)
        assert done is not None and done.parse_status == ParseStatus.DONE, (
            f"状态没到 done：{getattr(done, 'parse_status', None)} "
            f"error={getattr(done, 'parse_error', '')}\n" + "\n".join(log[-30:])
        )
        assert done.parse_attempts == 1
    finally:
        _stop_worker(worker)

    record.refresh_from_db()
    assert record.vector_status == "indexed"
    assert record.chunk_count > 0
    assert embedding_stub.requests > 0, "worker 应通过真实 HTTP 调用本地嵌入桩"

    client = vectorindex.build_client()
    try:
        assert vectorindex.count_points(client, collection) == record.chunk_count
        mine = [n for n in _list_collections(client) if n.startswith(namespace)]
        assert collection in mine  # 隔离自检
    finally:
        client.close()


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", CELERY_TASK_ALWAYS_EAGER=False)
def test_real_worker_duplicate_dispatch_processes_once(
    api, admin_user, created_library, namespace, queue_name, sender, cleanup, embedding_stub
):
    """派发两次：第二次走"已处理，跳过"，尝试号保持 1。"""
    record = _upload(api, admin_user, created_library, "dup.txt", "重复派发内容".encode())
    worker = _start_worker(queue_name, namespace, embedding_url=embedding_stub.base_url)
    try:
        log = _collect_worker_log(worker)
        assert " ready." in "\n".join(log), "worker 没就绪：\n" + "\n".join(log[-30:])

        first = _dispatch(sender, queue_name, record, created_library)
        cleanup["track"](first.id)
        _assert_state_success(first, log)
        done = _wait_db_state(record.pk, expect=ParseStatus.DONE)
        assert done is not None and done.parse_status == ParseStatus.DONE, "\n".join(log[-30:])
        assert done.parse_attempts == 1

        second = _dispatch(sender, queue_name, record, created_library)
        cleanup["track"](second.id)
        payload2 = _assert_state_success(second, log)
        assert payload2.get("metrics", {}).get("skipped") is True, payload2
        # 两次是**不同的 task_id**（拿 task_id 与 file_id 比较是无意义的）
        assert second.id != first.id
    finally:
        _stop_worker(worker)

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.parse_attempts == 1, record.parse_attempts


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", CELERY_TASK_ALWAYS_EAGER=False)
def test_worker_refuses_message_without_binding(
    api, admin_user, created_library, namespace, queue_name, sender, cleanup, embedding_stub
):
    """缺绑定信息的旧消息必须被**拒绝执行**（不能按主键猜着处理）。"""
    record = _upload(api, admin_user, created_library, "unbound.txt", "无绑定消息".encode())
    worker = _start_worker(queue_name, namespace, embedding_url=embedding_stub.base_url)
    try:
        log = _collect_worker_log(worker)
        assert " ready." in "\n".join(log), "worker 没就绪：\n" + "\n".join(log[-30:])
        result = sender.send_task(TASK_NAME, args=[record.pk], queue=queue_name)
        cleanup["track"](result.id)
        payload = result.get(timeout=WORKER_TIMEOUT, propagate=False)
        assert result.state == "SUCCESS", result.state
        assert payload is not None and payload["ok"] is False
        assert payload["reason"] == "missing_binding"
    finally:
        _stop_worker(worker)

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.PENDING, "被拒绝的消息不应改动资料状态"
    assert record.parse_attempts == 0


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", CELERY_TASK_ALWAYS_EAGER=False)
def test_worker_refuses_payload_bound_to_other_content(
    api, admin_user, created_library, namespace, queue_name, sender, cleanup, embedding_stub
):
    """绑定了别的指纹/库 → 拒绝处理（防跨库、跨内容误消费）。"""
    record = _upload(api, admin_user, created_library, "bind.txt", "绑定校验内容".encode())
    worker = _start_worker(queue_name, namespace, embedding_url=embedding_stub.base_url)
    try:
        log = _collect_worker_log(worker)
        assert " ready." in "\n".join(log), "worker 没就绪：\n" + "\n".join(log[-30:])
        result = sender.send_task(
            TASK_NAME,
            args=[record.pk],
            kwargs={"content_sha256": "0" * 64, "library_code": "some-other-library"},
            queue=queue_name,
        )
        cleanup["track"](result.id)
        payload = result.get(timeout=WORKER_TIMEOUT, propagate=False)
        assert payload is not None and payload["ok"] is False
        assert payload["reason"] == "payload_mismatch"
    finally:
        _stop_worker(worker)

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.PENDING
    assert record.parse_attempts == 0


def test_isolation_contract(queue_name, namespace, sender):
    """隔离契约自检（不需要 worker，纯配置断言）。"""
    assert queue_name.startswith("kb-test-")
    assert queue_name != "celery"
    assert TEST_BROKER_DB not in {"0", "1"}, "测试 broker DB 不能与业务 DB（0/1）相同"
    assert _broker_url().endswith(f"/{TEST_BROKER_DB}")
    # 命名空间必须**绑定到本次 session 的 run**：`kb-<session_uuid>-w<短随机>`。
    # 旧断言是 `startswith("kb-t-")` —— 那是 run 外前缀，清理器回收不到，
    # 已废弃。这里**不弱化**为"任意 kb"，而是精确核对 session UUID 与 run 前缀形态。
    session_uuid = os.environ.get("KB_TEST_RUN_UUID", "")
    assert session_uuid, "缺少 KB_TEST_RUN_UUID —— 会话隔离未生效"
    run_prefix = f"kb-{session_uuid}-"
    assert namespace.startswith(run_prefix), (
        f"命名空间 {namespace} 必须以本 session 前缀 {run_prefix} 开头"
        "（否则会话清理的「本 run 前缀清点」回收不到，会永久残留）"
    )
    assert not namespace.startswith("kb-t-"), (
        f"禁止回到已废弃的 run 外前缀 kb-t-：{namespace}"
    )
    # 必须是 run 前缀 + w<短随机> 的子域，而不是恰好等于前缀（那会与其它用例撞名）
    assert namespace != run_prefix.rstrip("-"), f"命名空间不能只是 run 前缀本身：{namespace}"
    assert len(namespace) > len(run_prefix) + 1, f"命名空间缺少子域后缀：{namespace}"
    assert _test_db_name(), "必须能取到 Django 实际使用的测试库名"
