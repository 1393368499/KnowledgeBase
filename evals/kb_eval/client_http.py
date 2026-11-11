"""在线模式 HTTP 检索客户端（可选）。

接口取自仓库真实路由 ``apps/knowledge/urls.py``：``POST /api/v1/knowledge/search/``
（``config/urls.py`` 把 ``apps.knowledge.urls`` 挂在 ``/api/v1/`` 下）。
请求体与 ``SearchRequestSerializer`` 对齐：``{"q": ..., "limit": 10, "libraries": [...]}``。
响应形如 ``{"query": ..., "results": [{file_id, text, score, original_name, library_code, ...}],
"report": {...}}``。

鉴权：``config/settings.py`` 的 DRF 默认类是 Session + Basic，且默认 ``IsAuthenticated``。
脚本化最省事的是 **HTTP Basic**：``--api-user/--api-password``（或环境变量
``KB_API_USER`` / ``KB_API_PASS``）。也支持 Bearer token（``token=...``）。

本模块只用标准库（urllib），不引入额外依赖。

注意：``run_eval.py --online`` **当前故意不接线**（Lead 2026-10-29 口径）——
真实栈的端到端验证由 ``evals/live_check.py`` 负责并产出
``evals/results/live-*.json``；本文件是接口契约的参考实现 + 自测，
供 live_check.py 参考或后续接回 CLI。
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

DEFAULT_TIMEOUT = 30.0
SEARCH_PATH = "/api/v1/knowledge/search/"
USER_AGENT = "kb-eval/1.0 (+offline-baseline)"


class OnlineError(RuntimeError):
    """在线检索不可用/响应不合法；消息必须可操作。"""


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class HttpSearchClient:
    """按 harness 契约形状返回结果的 HTTP 检索客户端。"""

    def __init__(
        self,
        api_base: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        libraries: Sequence[str] | None = None,
    ) -> None:
        base = str(api_base or "").strip().rstrip("/")
        if not base:
            raise OnlineError("缺少 --api-base（例如 http://127.0.0.1:8800）")
        if not base.startswith(("http://", "https://")):
            raise OnlineError(f"--api-base 必须是 http(s) URL，收到 {api_base!r}")
        self.api_base = base
        self.timeout = float(timeout)
        self.libraries = [str(item) for item in (libraries or [])]
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            headers["Authorization"] = _basic_auth_header(username, password)
        self._headers = headers

    # -- public ---------------------------------------------------------
    def search(self, query: str, limit: int = 10, *, timeout: float | None = None) -> dict:
        """检索一个查询，返回与离线 harness 同形状的 dict。"""
        payload: dict[str, Any] = {"q": str(query), "limit": int(limit)}
        if self.libraries:
            payload["libraries"] = list(self.libraries)
        started = time.perf_counter()
        data = self._post(payload, timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        results = data.get("results")
        if not isinstance(results, list):
            raise OnlineError(
                f"检索接口响应缺少 results 列表（实际：{type(results).__name__}）；"
                f"请确认 {SEARCH_PATH} 与当前代码版本一致"
            )
        shaped = [self._shape_result(item) for item in results]
        return {
            "results": shaped,
            "report": data.get("report") if isinstance(data.get("report"), dict) else {},
            "elapsed_ms": round(elapsed_ms, 3),
            "warnings": [],
            "elapsed_source": "client_wall",
        }

    def close(self) -> None:  # 与 harness 契约对齐；HTTP 无连接池需要释放
        return None

    # -- internals ------------------------------------------------------
    @staticmethod
    def _shape_result(item: Any) -> dict:
        if not isinstance(item, Mapping):
            raise OnlineError(f"results 里出现了非对象元素：{item!r}")
        file_id = item.get("file_id")
        doc_id = item.get("doc_id")
        if doc_id in (None, "") and file_id not in (None, ""):
            doc_id = str(file_id)
        return {
            "file_id": file_id,
            "doc_id": doc_id,
            "text": item.get("text") or "",
            "score": item.get("score"),
            "original_name": item.get("original_name") or "",
            "library_code": item.get("library_code") or "",
            "ordinal": item.get("ordinal"),
            "page": item.get("page"),
        }

    def _post(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> dict:
        url = urllib.parse.urljoin(self.api_base + "/", SEARCH_PATH.lstrip("/"))
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except OSError:
                pass
            if exc.code in (401, 403):
                raise OnlineError(
                    f"检索接口鉴权失败（HTTP {exc.code}）。需要 Basic 认证："
                    f"传 --api-user/--api-password（或环境变量 KB_API_USER/KB_API_PASS）；"
                    f"URL={url}；响应={detail}"
                ) from None
            raise OnlineError(f"检索接口返回 HTTP {exc.code}：{detail or exc.reason}；URL={url}") from None
        except urllib.error.URLError as exc:
            raise OnlineError(
                f"无法连接检索服务 {self.api_base}（{exc.reason}）。"
                f"在线模式需要先启动 Django（例如 `python manage.py runserver 127.0.0.1:8800`）"
                f"并保证数据库/向量库/模型服务可用；URL={url}"
            ) from None
        except TimeoutError:
            raise OnlineError(f"检索请求超时（>{timeout or self.timeout}s）；URL={url}") from None
        if status != 200:
            raise OnlineError(f"检索接口返回非 200 状态：{status}；URL={url}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OnlineError(f"检索接口返回的不是 JSON：{exc}；URL={url}") from None
        if not isinstance(data, dict):
            raise OnlineError(f"检索接口返回的 JSON 顶层不是对象：{type(data).__name__}；URL={url}")
        return data


def build_client(api_base: str, **kwargs: Any) -> HttpSearchClient:
    """工厂别名，便于 run_eval 里懒导入后一行构造。"""
    return HttpSearchClient(api_base, **kwargs)


# ---------------------------------------------------------------------------
# 自测：起一个本地假 HTTP 服务，验证请求形状 / 鉴权 / 响应整形 / 错误提示
# ---------------------------------------------------------------------------
def _run_self_test() -> int:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state: dict[str, Any] = {"expected": None, "last": None, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # 静音
            return None

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            state["last"] = {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(raw.decode("utf-8")),
            }
            if state["status"] == 401:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"detail":"Authentication credentials were not provided."}')
                return
            body = json.dumps(
                {
                    "query": state["last"]["body"]["q"],
                    "results": [
                        {"file_id": 11, "text": "报销流程见附件", "score": 0.93,
                         "original_name": "报销制度.md", "library_code": "policy"},
                        {"file_id": 12, "text": "另一段", "score": 0.5,
                         "original_name": "other.md", "library_code": "policy"},
                    ],
                    "report": {"collections": ["c1"], "reason": "ok"},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    failures: list[str] = []
    checks = 0

    def check(name: str, actual: Any, expected: Any) -> None:
        nonlocal checks
        checks += 1
        if actual != expected:
            failures.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
        else:
            print(f"PASS {name}")

    base = f"http://127.0.0.1:{port}"
    try:
        client = HttpSearchClient(base, username="u1", password="p1")
        result = client.search("报销怎么走", limit=7)
        check("returns 2 results", len(result["results"]), 2)
        check("elapsed_ms is float", isinstance(result["elapsed_ms"], float), True)
        check("shape file_id", result["results"][0]["file_id"], 11)
        check("shape doc_id fallback", result["results"][0]["doc_id"], "11")
        check("shape original_name", result["results"][0]["original_name"], "报销制度.md")
        check("report passthrough", result["report"]["reason"], "ok")
        check("request path", state["last"]["path"], "/api/v1/knowledge/search/")
        check("request body q", state["last"]["body"]["q"], "报销怎么走")
        check("request body limit", state["last"]["body"]["limit"], 7)
        check("basic auth header", state["last"]["auth"], "Basic dTE6cDE=")

        client2 = HttpSearchClient(base, token="tok-123")
        client2.search("x")
        check("bearer auth header", state["last"]["auth"], "Bearer tok-123")

        state["status"] = 401
        try:
            HttpSearchClient(base, username="u1", password="bad").search("x")
        except OnlineError as exc:
            check("401 gives actionable hint", "Basic 认证" in str(exc), True)
        else:
            check("401 gives actionable hint", "no raise", True)
        state["status"] = 200

        try:
            HttpSearchClient("ftp://nope")
        except OnlineError as exc:
            check("bad scheme rejected", "http(s)" in str(exc), True)
        else:
            check("bad scheme rejected", False, True)

        # 连接失败：用一个大概率没人监听的端口，验证提示里有启动命令
        try:
            HttpSearchClient("http://127.0.0.1:9", timeout=1.0).search("x")
        except OnlineError as exc:
            check("connection refused hint", "无法连接检索服务" in str(exc), True)
        else:
            check("connection refused hint", "no raise", True)
    finally:
        server.shutdown()
        server.server_close()

    print()
    if failures:
        for line in failures:
            print(line)
        print(f"FAILED: {len(failures)}/{checks} checks failed")
        return 1
    print(f"ALL PASS: {checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(_run_self_test())
