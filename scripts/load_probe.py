"""并发/负载探测脚本（**待执行**；本文件不含任何实测容量数字）。

设计口径（按审查要求重写）
--------------------------
旧版不可用，原因很具体，这里逐条修掉：

1. **认证**：项目所有接口都要登录。现在从环境变量取认证
   （``KB_LOAD_USER`` / ``KB_LOAD_PASSWORD``），先登录拿 session cookie，
   再带着 cookie 发请求；401/403 一律计为**失败**（不是"结果"）。
2. **upload 模式要真的传文件**：旧版只是 propose 一个 ``upload_file`` 操作
   （那个操作本身要求用户在页面亲自选文件）。现在用 **multipart 真上传**
   一个小测试文件到**明确隔离的 load-test 库**，再按需跑完整流水线。
3. **confirm 模式要有合法一次性凭证**：旧版传空 token，必然全失败。
   现在先 propose 拿到 ``confirm_token``，再用**同一个 token** 并发确认，
   验证"一次性凭证 + CAS"确实只允许一个成功。
4. **execute 模式只读**：并发执行只读操作（如 ``get_stats``），
   验证执行端 CAS 不会重复执行。
5. **安全**：精确解析 hostname，只允许 **127.0.0.1 / localhost / ::1**；
   并要求目标服务是"**模型已禁用**的独立测试服务"
   （``KB_MODEL_CALLS_DISABLED=1``），避免本机检索间接把压力打到 AS711；
   否则拒绝运行。
6. **统计**：成功率、吞吐、P50/P95/P99、网络异常分类。

用法：

    $env:KB_LOAD_BASE='http://127.0.0.1:8011'
    $env:KB_LOAD_USER='loadbot'
    $env:KB_LOAD_PASSWORD='<测试账号口令>'
    $env:KB_LOAD_LIBRARY='load-test'          # 必须是一个隔离的测试库 code
    $env:KB_LOAD_ALLOW_WRITE='1'              # upload 模式必须显式打开
    .\\.venv\\Scripts\\python.exe scripts/load_probe.py search  --concurrency 8 --seconds 20
    .\\.venv\\Scripts\\python.exe scripts/load_probe.py upload  --concurrency 4 --files 40
    .\\.venv\\Scripts\\python.exe scripts/load_probe.py confirm --concurrency 8 --file-id 123
    .\\.venv\\Scripts\\python.exe scripts/load_probe.py execute --concurrency 8 --intent get_stats

跑完必须**原样回填**输出的实测数字；不要凭印象写容量。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ------------------------------------------------------------------ 统计 ----


@dataclass
class Stats:
    """成功率 / 吞吐 / 延迟分位 / 网络异常分类（线程安全）。"""

    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    latencies: list[float] = field(default_factory=list)
    ok: int = 0
    failed: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    network_errors: dict[str, int] = field(default_factory=dict)
    degraded: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(
        self,
        seconds: float,
        *,
        status: int | None,
        ok: bool,
        degraded: bool = False,
        network_error: str = "",
    ) -> None:
        with self._lock:
            self.latencies.append(seconds)
            if network_error:
                self.network_errors[network_error] = (
                    self.network_errors.get(network_error, 0) + 1
                )
            if status is not None:
                self.status_counts[str(status)] = self.status_counts.get(str(status), 0) + 1
            if ok:
                self.ok += 1
            else:
                self.failed += 1
            if degraded:
                self.degraded += 1

    def summary(self) -> dict:
        self.finished_at = self.finished_at or time.time()
        elapsed = max(self.finished_at - self.started_at, 1e-6)
        with self._lock:
            values = sorted(self.latencies)
            total = self.ok + self.failed

        def pick(ratio: float) -> float:
            if not values:
                return 0.0
            index = min(len(values) - 1, int(len(values) * ratio))
            return round(values[index], 4)

        return {
            "elapsed_seconds": round(elapsed, 2),
            "requests": total,
            "ok": self.ok,
            "failed": self.failed,
            "success_rate": round(self.ok / total, 4) if total else 0.0,
            "throughput_rps": round(total / elapsed, 2) if total else 0.0,
            "latency_seconds": {
                "p50": pick(0.50),
                "p95": pick(0.95),
                "p99": pick(0.99),
                "mean": round(statistics.fmean(values), 4) if values else 0.0,
                "max": round(values[-1], 4) if values else 0.0,
            },
            "status_counts": dict(sorted(self.status_counts.items())),
            "network_errors": dict(sorted(self.network_errors.items())),
            "degraded_responses": self.degraded,
        }


# ------------------------------------------------------------- HTTP 客户端 ----


class Session:
    """带 cookie 的极简 HTTP 客户端（登录一次，全线程复用 cookie）。"""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf = ""

    # -- 请求 --------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        form: dict | None = None,
        files: dict | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, dict]:
        url = self.base + path
        headers: dict[str, str] = {}
        data: bytes | None = None

        if files is not None:
            boundary = "----kb-load-" + uuid.uuid4().hex
            data = _encode_multipart(boundary, form or {}, files)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        if method not in {"GET", "HEAD"} and self.csrf:
            headers["X-CSRFToken"] = self.csrf
            headers["Referer"] = self.base + "/"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                self._capture_csrf()
                return response.status, _json_or_raw(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            self._capture_csrf()
            return exc.code, _json_or_raw(raw)

    def _capture_csrf(self) -> None:
        for cookie in self.jar:
            if cookie.name == "csrftoken":
                self.csrf = cookie.value

    def login(self, username: str, password: str) -> None:
        """先 GET 登录页拿 CSRF，再 POST 表单登录。"""
        self.request("GET", "/app/login/")
        status, _body = self.request(
            "POST",
            "/app/login/",
            form={"username": username, "password": password},
        )
        if status not in {200, 302}:
            raise SystemExit(f"登录失败（HTTP {status}）：请检查 KB_LOAD_USER/PASSWORD")
        # 用只读接口确认会话真的生效
        probe, _ = self.request("GET", "/api/v1/knowledge/libraries/")
        if probe in {401, 403}:
            raise SystemExit(f"登录后仍无权限（HTTP {probe}）：账号或口令不对")


def _encode_multipart(boundary: str, form: dict, files: dict) -> bytes:
    lines: list[bytes] = []
    for name, value in form.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))
    for name, (filename, content) in files.items():
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode()
        )
        lines.append(f"Content-Type: {ctype}".encode())
        lines.append(b"")
        lines.append(content)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    return b"\r\n".join(lines)


def _json_or_raw(raw: str) -> dict:
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw[:300]}
    return body if isinstance(body, dict) else {"data": body}


# ----------------------------------------------------------------- 场景 ----


def _record(stats: Stats, started: float, status: int | None, body: dict, *, network: str = "") -> None:
    """统一记录：**401/403 一律算失败**（认证/权限问题不是"压力结果"）。"""
    seconds = time.time() - started
    ok = bool(status) and 200 <= int(status) < 300
    if status in {401, 403}:
        ok = False
    report = (body or {}).get("report") or {}
    stats.record(
        seconds,
        status=status,
        ok=ok,
        degraded=bool(report.get("degraded")),
        network_error=network,
    )


def run_search(session: Session, stats: Stats, *, concurrency: int, seconds: int, query: str) -> None:
    stop_at = time.time() + seconds

    def worker() -> None:
        while time.time() < stop_at:
            started = time.time()
            try:
                status, body = session.request(
                    "POST", "/api/v1/knowledge/search/", payload={"q": query, "limit": 10}
                )
            except Exception as exc:  # noqa: BLE001 - 网络异常单独计数
                _record(stats, started, None, {}, network=type(exc).__name__)
                continue
            _record(stats, started, status, body)

    _run_threads(worker, concurrency)


def run_upload(
    session: Session,
    stats: Stats,
    *,
    concurrency: int,
    files: int,
    library: str,
    allow_write: bool,
) -> None:
    """**真实 multipart 上传**到隔离的 load-test 库。"""
    if not allow_write:
        raise SystemExit(
            "upload 模式必须显式设置 KB_LOAD_ALLOW_WRITE=1（会在目标库里真的写入数据）"
        )
    # 库必须**压测专用**（load-test- 前缀），否则拒绝——防止写进业务库
    assert_load_test_library(library)
    lock = threading.Lock()
    remaining = {"n": files}

    def worker() -> None:
        while True:
            with lock:
                if remaining["n"] <= 0:
                    return
                index = files - remaining["n"]
                remaining["n"] -= 1
            payload = (
                f"load-test document {index}\n"
                f"标识 {uuid.uuid4().hex}\n"
                "质保期 12 个月，预付款比例 30%。\n"
            ).encode("utf-8")
            started = time.time()
            try:
                status, body = session.request(
                    "POST",
                    "/api/v1/knowledge/files/upload/",
                    form={"library": library},
                    files={"file": (f"load-{index}.txt", payload)},
                )
            except Exception as exc:  # noqa: BLE001
                _record(stats, started, None, {}, network=type(exc).__name__)
                continue
            _record(stats, started, status, body)

    _run_threads(worker, concurrency)


def run_confirm(
    session: Session, stats: Stats, *, concurrency: int, file_id: int
) -> None:
    """用一个**合法的一次性 token** 并发确认：期望只有一个成功（CAS）。"""
    status, body = session.request(
        "POST",
        "/api/v1/planner/operations/propose/",
        payload={"intent": "parse_file", "params": {"file_id": file_id}},
    )
    if status not in {200, 201} or not body.get("confirm_token"):
        raise SystemExit(f"提案失败，无法取得确认凭证：HTTP {status} {body}")
    operation_id = body["operation_id"]
    token = body["confirm_token"]
    print(f"[confirm] operation={operation_id}（用同一个 token 并发 {concurrency} 次）")

    def worker() -> None:
        started = time.time()
        try:
            code, response = session.request(
                "POST",
                f"/api/v1/planner/operations/{operation_id}/confirm/",
                payload={"token": token, "typed_name": ""},
            )
        except Exception as exc:  # noqa: BLE001
            _record(stats, started, None, {}, network=type(exc).__name__)
            return
        _record(stats, started, code, response)

    _run_threads(worker, concurrency)


def run_execute(
    session: Session, stats: Stats, *, concurrency: int, intent: str, params_json: str
) -> None:
    """并发执行**只读**操作（默认 get_stats），验证执行端不会重复执行。"""
    params = json.loads(params_json) if params_json else {}
    # **白名单 + 服务端目录交叉核对**：压测绝不允许触发写操作
    assert_read_only_intent(intent, session)
    status, body = session.request(
        "POST",
        "/api/v1/planner/operations/propose/",
        payload={"intent": intent, "params": params},
    )
    if status not in {200, 201}:
        raise SystemExit(f"提案失败：HTTP {status} {body}")
    operation_id = body["operation_id"]
    token = body.get("confirm_token", "")

    # 只读操作需要一次确认才进入 ARMED
    if token:
        code, confirm = session.request(
            "POST",
            f"/api/v1/planner/operations/{operation_id}/confirm/",
            payload={"token": token, "typed_name": ""},
        )
        if code not in {200, 202}:
            raise SystemExit(f"确认失败，无法进入可执行状态：HTTP {code} {confirm}")

    def worker() -> None:
        started = time.time()
        try:
            code, response = session.request(
                "POST",
                f"/api/v1/planner/operations/{operation_id}/execute/",
                payload={},
            )
        except Exception as exc:  # noqa: BLE001
            _record(stats, started, None, {}, network=type(exc).__name__)
            return
        _record(stats, started, code, response)

    _run_threads(worker, concurrency)


def _run_threads(worker, concurrency: int) -> None:
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# ------------------------------------------------------------------ 入口 ----


def assert_local_only(base: str) -> None:
    """**精确**解析 hostname，只允许本机（AS711 是共享服务，绝不能压）。"""
    parsed = urllib.parse.urlparse(base)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit(f"拒绝执行：只支持 http/https，收到 {parsed.scheme!r}")
    if host not in LOCAL_HOSTS:
        raise SystemExit(
            f"拒绝执行：只允许压测本机（127.0.0.1/localhost/::1），收到 hostname={host!r}"
        )


def assert_models_disabled(base: str, session: Session) -> None:
    """**fail-closed**：服务端必须自述"模型已禁用 **且** 是负载测试实例"，否则拒绝发压。

    旧版只是打印一句提示就继续——那不是保护。现在读服务端 ``/guide/`` 的
    ``environment`` 字段（服务端事实，不是本地猜测），两个条件都必须为真：

    1. ``model_calls_disabled is True``：否则检索/写入会**真的**调用 AS711；
    2. ``load_test_mode is True``：这是**显式声明**的压测实例。
       正式服务（含本地 8000 的真实模型配置）默认是 False → 直接拒绝，
       从机制上防止"顺手拿正式服务压测"。

    另外把隔离证据（库名/向量前缀/是否禁止派发）打印出来，便于确认压的是哪一套。
    """
    status, body = session.request("GET", "/api/v1/planner/guide/")
    if status != 200:
        raise SystemExit(f"无法读取服务端环境自述（HTTP {status}），拒绝发压")
    environment = (body or {}).get("environment") or {}
    if not environment:
        raise SystemExit(
            "服务端没有返回 environment 自述（可能是旧版本），无法确认隔离状态，拒绝发压"
        )

    disabled = environment.get("model_calls_disabled")
    load_test = environment.get("load_test_mode")
    print("[env] 目标服务自述：", json.dumps(environment, ensure_ascii=False))

    if disabled is not True:
        raise SystemExit(
            "拒绝发压：目标服务 model_calls_disabled 不是 True"
            f"（实际 {disabled!r}）。压测会真的调用模型服务（AS711）。"
        )
    if load_test is not True:
        raise SystemExit(
            "拒绝发压：目标服务 load_test_mode 不是 True"
            f"（实际 {load_test!r}）。这不是声明过的负载测试实例——"
            "正式服务（如 8000 真实模型配置）不能用来压测。"
            "请用 config.loadtest_settings 起一套测试服务。"
        )
    if environment.get("dispatch_disabled") is not True:
        raise SystemExit(
            "拒绝发压：目标服务没有禁止任务派发（dispatch_disabled != True），"
            "压测产生的任务可能流进业务队列。"
        )
    prefix = str(environment.get("qdrant_collection_prefix") or "")
    # **只认本 run 的独占前缀**：目标服务必须由本次压测以
    # `KB_LOADTEST_QDRANT_PREFIX=kb-<run_uuid>` 启动。以前这里只要求
    # `kb-lt-` 开头 —— 那是"谁都能用"的共享前缀，正是历史 42 GiB 里
    # 125 个 `kb-lt-*` 集合清不掉的成因（不属于任何 run，清理器按设计拒绝碰）。
    run_prefix = os.environ.get("KB_LOAD_RUN_PREFIX", "")
    if not run_prefix:
        raise SystemExit(
            "拒绝发压：缺少 KB_LOAD_RUN_PREFIX（应由 load_probe.main 自动注入）。"
        )
    if prefix != run_prefix:
        raise SystemExit(
            f"拒绝发压：目标服务自述的向量前缀 {prefix!r} 与本次 run 的 {run_prefix!r} **不精确相等**。"
            "请用本次压测生成的 KB_LOADTEST_QDRANT_PREFIX 启动测试服务；"
            "共享的 kb-lt-* 前缀已被禁止（无法按 run 回收）。"
        )
    print(
        "[env] 隔离确认通过：models=disabled, load_test_mode=True, "
        f"dispatch=disabled, qdrant_prefix={prefix}（本 run）"
    )


#: 允许并发执行的**只读**操作白名单。
#:
#: 为什么用白名单而不是黑名单：`execute` 是"真的会执行操作"的入口，
#: 用黑名单意味着"新增一个我没见过的写操作"会被放进去跑。白名单 fail-closed。
READ_ONLY_INTENTS = {
    "get_stats",
    "guide",
    "list_audit",
    "list_libraries",
    "list_files",
    "read_file",
    "search_knowledge",
    "test_model_services",
    "embedding_profiles",
    "index_stats",
}


def assert_read_only_intent(intent: str, session: Session) -> None:
    """`execute` 只允许只读意图；并用服务端目录交叉核对 ``is_mutation``。"""
    if intent not in READ_ONLY_INTENTS:
        raise SystemExit(
            f"拒绝执行：{intent!r} 不在只读白名单里。"
            f"压测不得触发写操作（白名单：{sorted(READ_ONLY_INTENTS)}）"
        )
    status, body = session.request("GET", "/api/v1/planner/catalog/")
    if status != 200:
        raise SystemExit(f"无法读取能力目录（HTTP {status}），拒绝执行")
    for row in (body or {}).get("results") or []:
        if row.get("intent") == intent and row.get("is_mutation"):
            raise SystemExit(
                f"拒绝执行：服务端把 {intent!r} 标记为写操作（is_mutation=True），"
                "白名单与目录不一致，按更严格的一方处理。"
            )


def assert_load_test_library(library: str) -> None:
    """upload 只能写**压测专用库**（``load-test-`` 前缀），防止写进业务库。"""
    if not library:
        raise SystemExit("upload 模式需要 KB_LOAD_LIBRARY=<库 code>")
    if not library.startswith("load-test-"):
        raise SystemExit(
            f"拒绝上传：库 code {library!r} 不是压测专用库（必须以 'load-test-' 开头）。"
            "压测数据不允许写进业务信息库。"
        )


import re as _re

#: run-id 严格格式：12 或 32 位小写 hex（与 RunResources / loadtest_env 一致）
_RUN_ID_RE = _re.compile(r"^[0-9a-f]{12}$|^[0-9a-f]{32}$")


def _start_load_run(run_id: str):
    """按**预生成的 run-id** 建本次压测 run（不再自己随机生成后要求服务去匹配）。

    为什么必须由调用方给 run-id：测试服务要先启动、并把它自己的向量前缀自述给
    客户端；如果客户端在这里随机生成一个新 uuid，服务**不可能**预先知道它，
    于是"自动生成后永远拒绝"。所以 run-id 由 `loadtest_env.py --run-id` 预生成，
    同一个值喂给服务与客户端。
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import run_resources as rr

    if not _RUN_ID_RE.match(run_id or ""):
        raise SystemExit(
            "--run-id 必须是 12/32 位小写 hex（与 loadtest_env.py --run-id 用同一个值）"
        )
    os.environ["KB_LOAD_RUN_ID"] = run_id
    os.environ["KB_LOAD_RUN_PREFIX"] = f"kb-{run_id}"
    endpoint = os.environ.get("KB_QDRANT_URL", "").strip()
    run = rr.RunResources.new(endpoint=endpoint, identity="load-probe", run_uuid=run_id)
    print(f"[run] run={run_id} 本 run 前缀={os.environ['KB_LOAD_RUN_PREFIX']}"
          + (f" qdrant={endpoint}" if endpoint else " qdrant=(未设置，结束时只能清本地目录)"))
    return run


def _finish_load_run(run) -> None:
    """压测结束（含 Ctrl+C / 异常）统一清理：只删**本 run 前缀**的集合与本地目录。"""
    endpoint = os.environ.get("KB_QDRANT_URL", "").strip()
    try:
        report = run.cleanup(sweep_run_prefix=bool(endpoint))
        print(f"[run] 清理完成：deleted={len(report['deleted'])} failed={len(report['failed'])}")
        for item in report["deleted"]:
            print(f"       - {item}")
    finally:
        run.release()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="并发探测（只允许本机）")
    parser.add_argument("mode", choices=["search", "upload", "confirm", "execute"])
    parser.add_argument("--base", default=os.environ.get("KB_LOAD_BASE", "http://127.0.0.1:8800"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--files", type=int, default=20)
    parser.add_argument("--file-id", type=int, default=0)
    parser.add_argument("--intent", default="get_stats")
    parser.add_argument("--params", default="")
    parser.add_argument("--query", default="质保期")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--run-id",
        default=os.environ.get("KB_LOAD_RUN_ID", ""),
        help="预生成的 run-id（与 loadtest_env.py --run-id 同一个值）。"
             "必须与目标服务自述的向量前缀精确一致，否则拒绝发压",
    )
    args = parser.parse_args(argv)

    # **受管模式**（由 loadtest_runner 设 KB_LOAD_MANAGED=1 时）：
    # 资源生命周期归父 runner 所有 —— 这里**不能**再 RunResources.new，
    # 否则会撞父进程已持有的 run 锁（实测报"该 run 已被另一个进程持有"），
    # 也不能自己 cleanup（那会在服务还活着时先删资源）。
    managed = os.environ.get("KB_LOAD_MANAGED") == "1"
    if managed:
        # 父 runner 只传 run-id；这里补齐它依赖的前缀变量（不新建 run）
        run_id = args.run_id or os.environ.get("KB_LOAD_RUN_ID", "")
        if not _RUN_ID_RE.match(run_id or ""):
            raise SystemExit("受管模式下缺少合法 run-id（KB_LOAD_RUN_ID 或 --run-id）")
        os.environ.setdefault("KB_LOAD_RUN_PREFIX", f"kb-{run_id}")
        print(f"[run] 受管模式：run={run_id} 前缀={os.environ['KB_LOAD_RUN_PREFIX']}"
              "（资源由父 runner 收尾）")
        return _run_once(args)

    # 非受管：自己拥有生命周期，**在 main 内部** try/finally（程序化调用也覆盖）。
    run = _start_load_run(args.run_id)
    try:
        return _run_once(args)
    finally:
        _finish_load_run(run)


def _run_once(args) -> int:
    assert_local_only(args.base)

    username = os.environ.get("KB_LOAD_USER", "")
    password = os.environ.get("KB_LOAD_PASSWORD", "")
    if not username or not password:
        raise SystemExit("请设置 KB_LOAD_USER / KB_LOAD_PASSWORD（测试账号）")

    session = Session(args.base)
    session.login(username, password)
    assert_models_disabled(args.base, session)

    stats = Stats()
    if args.mode == "search":
        run_search(
            session, stats, concurrency=args.concurrency, seconds=args.seconds, query=args.query
        )
    elif args.mode == "upload":
        run_upload(
            session,
            stats,
            concurrency=args.concurrency,
            files=args.files,
            library=os.environ.get("KB_LOAD_LIBRARY", ""),
            allow_write=os.environ.get("KB_LOAD_ALLOW_WRITE") == "1",
        )
    elif args.mode == "confirm":
        if not args.file_id:
            raise SystemExit("confirm 模式需要 --file-id <已入库资料 id>")
        run_confirm(session, stats, concurrency=args.concurrency, file_id=args.file_id)
    else:
        run_execute(
            session,
            stats,
            concurrency=args.concurrency,
            intent=args.intent,
            params_json=args.params,
        )

    summary = stats.summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        "\n注意：以上是**实测**结果，请原样回填文档；"
        "不要凭印象写容量数字。success_rate 里 401/403 计为失败。",
        file=sys.stderr,
    )
    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    # 生命周期已经**在 main 内部**（建 run → try/finally 清理），这里不再重复管理，
    # 否则会对同一个 run 建两次、且 `_start_load_run()` 缺 run_id 直接 TypeError。
    raise SystemExit(main(sys.argv[1:]))
