"""按 run 登记与自动清理测试资源（最小实现）。

问题：测试/压测会创建 Qdrant 集合、原件目录、Redis 键等资源，**失败或被中断时
不会清理**，日积月累（当前实测已有 191 个集合、125 个 `kb-lt-*` 占 42 GiB）。

设计原则（安全优先）：
  1. **每次 run 独占前缀**（`kb-{run_uuid}-`）与**自己的资源清单**；
  2. **先登记意图（pending）→ 创建 → 记创建成功（created）**；
  3. 清理**只**删"本次清单里、状态 created、且 endpoint/identity 都匹配"的资源；
  4. **禁止**任何 `kb-*` / `kb-lt-*` 模糊前缀扫删 —— 只按**精确 key** 删；
  5. 删除前把目标 `resolve()` 成真实绝对路径，必须落在允许根之内，拒绝链接越界；
  6. 受保护对象（如生产集合 `kb-1-g1`）**永不删除**；
  7. 被杀进程来不及 finally 时，清单留在盘上；后续用 `recover` 显式恢复清理，
     且必须确认没有**活跃 run**（心跳新鲜）才允许动；
  8. 清理幂等：已删过的跳过；失败则**非零退出 + 显式报告**。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import threading
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "artifacts" / "test-runs"
#: 心跳新鲜度阈值：超过它就认为该 run 已死（可被 recover 处理）
HEARTBEAT_STALE_SECONDS = 900
#: **永不删除**的集合名（生产/主实例的关键对象）
PROTECTED_NAMES = frozenset({"kb-1-g1"})
#: 受保护前缀：生产集合前缀，绝不允许被测试清理碰到
PROTECTED_PREFIXES = ("kb-1-", "kb-2-", "kb-3-g", "kb-9-g")
KINDS = ("qdrant_collection", "file", "dir", "redis_key")
#: Redis：**禁止** DB0（业务库），只允许专用隔离 DB
FORBIDDEN_REDIS_DB = 0
#: run_uuid 严格格式：我们生成 uuid4().hex[:12]；也接受完整 32 位 hex。
#: **禁止**任何路径分隔符与 `..` —— 否则 `--uuid ../..` 能逃出 runs 根。
RUN_UUID_RE = re.compile(r"^[0-9a-f]{12}$|^[0-9a-f]{32}$")
#: 每个 run 一把进程内锁（跨进程另有唯一 tmp 名兜底）
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _acquire_os_lock(handle) -> bool:
    """尝试**非阻塞**独占 OS 锁；成功返回 True。跨平台（Windows/POSIX）。"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _lock_for(run_uuid: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(run_uuid, threading.Lock())


def assert_valid_run_uuid(run_uuid: str) -> str:
    if not RUN_UUID_RE.match(run_uuid or ""):
        raise CleanupError(f"run_uuid 格式非法（只允许 12/32 位小写 hex）：{run_uuid!r}")
    return run_uuid


def resolve_run_dir(run_uuid: str, runs_dir: Path) -> Path:
    """把 run_uuid 解析成 runs 根之下的**真实**子目录，拒绝越界。"""
    assert_valid_run_uuid(run_uuid)
    base = Path(os.path.realpath(runs_dir))
    target = Path(os.path.realpath(base / run_uuid))
    if os.path.commonpath([str(target), str(base)]) != str(base) or target == base:
        raise CleanupError(f"run 目录越界：{target} 不在 {base} 之下")
    return target


class CleanupError(RuntimeError):
    """清理失败：调用方必须转成非零退出并显式报告。"""


def _norm_endpoint(url: str) -> tuple[str, str, int, str]:
    """把端点规范化成 (scheme, host, port, path) 用于**精确**比较。

    补默认端口、统一小写、去尾部斜杠；绝不做 substring 包含判断。
    """
    parts = urlsplit(url if "://" in url else f"http://{url}")
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    path = (parts.path or "").rstrip("/")
    return (scheme, host, port, path)


# ------------------------------------------------- 进程存活：**只读**查询 ----
#: 明确表示"该 PID 不存在"的 Win32 错误码；其余（含拒绝访问）一律保守视为存活
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


def pid_start_token(pid: int) -> float | None:
    """返回进程创建时间（Windows）/ None（其它平台或查询失败）——**只读**。

    用作身份令牌：PID 会被系统复用，只比 PID 不足以确认"还是那个进程"。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            creation, _exit, _kernel, _user = (
                wintypes.FILETIME(), wintypes.FILETIME(),
                wintypes.FILETIME(), wintypes.FILETIME(),
            )
            ok = k32.GetProcessTimes(handle, ctypes.byref(creation),
                                     ctypes.byref(_exit), ctypes.byref(_kernel),
                                     ctypes.byref(_user))
            if not ok:
                return None
            return (creation.dwHighDateTime << 32 | creation.dwLowDateTime) / 1e7
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def pid_is_alive_readonly(pid: int) -> bool:
    """**只读**判断进程是否存活；不发送任何信号。

    保守原则：查询失败/拒绝访问/平台不支持 → 返回 **True**（视为存活），
    宁可拒绝清理也不误删；只有明确 `ERROR_INVALID_PARAMETER` 才判为不存在。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True                      # 参数不可信 → 保守视为存活
    if pid <= 0:
        return True
    if os.name != "nt":
        # POSIX：/proc 只读查看，绝不 os.kill
        try:
            return os.path.exists(f"/proc/{pid}")
        except OSError:
            return True
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            # 只有"参数无效"才证明不存在；拒绝访问等一律保守视为存活
            return err != _ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True              # 查询失败 → 保守视为存活
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return True                      # 未知 → 保守视为存活


def _now() -> float:
    return time.time()


def _atomic_write(path: Path, text: str) -> None:
    """原子写：**唯一** tmp 名（并发写不会撞同一个临时文件）。"""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _is_link(path: Path) -> bool:
    """是否符号链接/junction（Windows 上 junction 也需要识别）。"""
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)  # 目录联接
    except OSError:
        return False


class RunResources:
    """一次 run 的资源登记表 + 清理器。"""

    def __init__(
        self,
        run_uuid: str,
        *,
        endpoint: str,
        identity: str,
        dir_path: Path,
        allow_path_roots: list[Path] | None = None,
    ) -> None:
        self.run_uuid = assert_valid_run_uuid(run_uuid)
        self.dir = dir_path
        self.manifest = self.dir / "manifest.json"
        self.heartbeat = self.dir / "heartbeat"
        self.endpoint = endpoint
        self.identity = identity
        # 允许删除的路径根：**只**允许本 run 自己的目录（不接受任意放宽）
        self.allowed_roots = [Path(os.path.realpath(self.dir))]
        self._lock = _lock_for(self.run_uuid)

    @classmethod
    def new(
        cls,
        *,
        endpoint: str,
        identity: str,
        runs_dir: Path | None = None,
        run_uuid: str | None = None,
    ) -> "RunResources":
        """**新建**一次 run：建目录、写清单、打心跳。"""
        base = runs_dir or RUNS_DIR
        # **先校验 uuid，再 mkdir**：否则 `../evil` 之类会先建出目录再报错。
        rid = assert_valid_run_uuid(run_uuid or uuid.uuid4().hex[:12])
        base.mkdir(parents=True, exist_ok=True)
        obj = cls(rid, endpoint=endpoint, identity=identity,
                  dir_path=resolve_run_dir(rid, base))
        obj.dir.mkdir(parents=True, exist_ok=True)
        if not obj.manifest.exists():
            obj._write({"run_uuid": rid, "endpoint": endpoint,
                        "identity": identity, "entries": []})
        # **持有**跨进程 OS 锁直到本对象释放：别人靠它判断我们是否还活着
        obj._lock_handle = (obj.dir / "run.lock").open("a+b")
        if not _acquire_os_lock(obj._lock_handle):
            raise CleanupError(
                f"该 run 已被另一个进程持有：{rid}（拒绝重复占用同一 run_uuid）"
            )
        obj._write_owner()
        obj.beat()
        return obj

    def _write_owner(self) -> None:
        """记录持有者身份（pid + 进程创建时间），便于人工排查。"""
        doc = self._read()
        doc["owner"] = {"pid": os.getpid(), "started_at": _now(), "host": socket.gethostname()}
        self._write(doc)

    def release(self) -> None:
        """释放 OS 锁（正常结束时调用；进程被杀时由 OS 自动释放）。"""
        handle = getattr(self, "_lock_handle", None)
        if handle is not None:
            _release_os_lock(handle)
            try:
                handle.close()
            except OSError:
                pass
            self._lock_handle = None

    @classmethod
    def open(cls, run_uuid: str, *, runs_dir: Path | None = None) -> "RunResources":
        """**只读打开**已有 run：不 mkdir、不 beat（读清单不能改变状态）。

        这正是原实现的根因：recover 一读就刷新心跳 → 永远判"存活"。
        """
        base = runs_dir or RUNS_DIR
        rid = assert_valid_run_uuid(run_uuid)
        dir_path = resolve_run_dir(rid, base)
        doc_path = dir_path / "manifest.json"
        if not doc_path.exists():
            raise CleanupError(f"没有该 run 的清单：{doc_path}")
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        return cls(rid, endpoint=doc.get("endpoint", ""),
                   identity=doc.get("identity", ""), dir_path=dir_path)

    # ------------------------------------------------------------ 内部 ----
    def _read(self) -> dict[str, Any]:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def _write(self, doc: dict[str, Any]) -> None:
        _atomic_write(self.manifest, json.dumps(doc, ensure_ascii=False, indent=2))

    def beat(self) -> None:
        """心跳：recover 靠它判断这个 run 是否还活着。"""
        _atomic_write(self.heartbeat, json.dumps({"at": _now(), "pid": os.getpid()}))

    def is_alive(self, stale_seconds: int = HEARTBEAT_STALE_SECONDS) -> bool:
        """**以跨进程 OS 锁为准**：能拿到锁 → 持有者已死（心跳只是辅助信息）。

        心跳单独不可信：长测试可能很久不 beat（被误判为死），而被杀的 run
        可能留下新鲜心跳（被误判为活）。
        """
        lock_path = self.dir / "run.lock"
        if lock_path.exists():
            with lock_path.open("a+b") as handle:
                if not _acquire_os_lock(handle):
                    return True          # 锁被持有 → 确实还活着
                _release_os_lock(handle)
                return False             # 锁空闲 → 持有者已死
        # 没有锁文件（旧版本遗留）：退回心跳，并在报告里说明依据
        if not self.heartbeat.exists():
            return False
        try:
            at = float(json.loads(self.heartbeat.read_text(encoding="utf-8"))["at"])
        except Exception:  # noqa: BLE001
            return False
        return (_now() - at) < stale_seconds

    def set_meta(self, **meta: Any) -> None:
        """写入 run 级元数据（如本 runner 启动的服务 PID / 独占库名）。

        `recover` 会读它做**身份验证**：只凭"父锁已释放"不能认定服务/worker 已死。
        """
        with self._lock:
            doc = self._read()
            doc.setdefault("meta", {}).update(meta)
            self._write(doc)

    def get_meta(self) -> dict[str, Any]:
        return dict(self._read().get("meta") or {})

    # ------------------------------------------------------- 独占前缀 ----
    @property
    def prefix(self) -> str:
        """本次 run 独占的集合名前缀（也用于隔离 identity）。"""
        return f"kb-{self.run_uuid}-"

    def name(self, suffix: str) -> str:
        """生成一个**本 run 专属**的集合名。"""
        safe = "".join(ch for ch in suffix if ch.isalnum() or ch in "-_")[:40]
        return f"{self.prefix}{safe}"

    # ------------------------------------------------- 登记 / 创建确认 ----
    def declare(self, kind: str, key: str, **meta: Any) -> None:
        """**创建之前**登记意图（status=pending）。加锁 + 锁内重读，避免丢更新。"""
        if kind not in KINDS:
            raise ValueError(f"未知资源类型：{kind}")
        with self._lock:
            doc = self._read()          # 锁内重读：并发下才不会覆盖别人的登记
            if any(e["kind"] == kind and e["key"] == key for e in doc["entries"]):
                return
            doc["entries"].append(
                {"kind": kind, "key": key, "status": "pending",
                 "endpoint": self.endpoint, "identity": self.identity,
                 "declared_at": _now(), **meta}
            )
            self._write(doc)

    def confirm(self, kind: str, key: str) -> None:
        """创建**成功之后**标记 created（只有 created 才会被清理）。"""
        with self._lock:
            return self._confirm_locked(kind, key)

    def _confirm_locked(self, kind: str, key: str) -> None:
        doc = self._read()
        for entry in doc["entries"]:
            if entry["kind"] == kind and entry["key"] == key:
                entry["status"] = "created"
                entry["created_at"] = _now()
                self._write(doc)
                return
        raise CleanupError(f"未登记的 {kind}:{key}，拒绝确认（先 declare）")

    # ---------------------------------------------------------- 清理 ----
    def _entry_bound(self, entry: dict[str, Any]) -> bool:
        """登记项是否绑定到**本次** run 的 endpoint + identity。"""
        return (
            entry.get("endpoint") == self.endpoint
            and entry.get("identity") == self.identity
        )

    def _check_collection_allowed(self, key: str) -> None:
        if key in PROTECTED_NAMES:
            raise CleanupError(f"受保护对象，拒绝删除：{key}")
        if key.startswith(PROTECTED_PREFIXES):
            raise CleanupError(f"生产前缀，拒绝删除：{key}")
        # **必须**是本 run 独占前缀 + 精确登记名；杜绝 `kb-*`/`kb-lt-*` 模糊扫删
        if not key.startswith(self.prefix):
            raise CleanupError(f"不属于本次 run 的前缀（{self.prefix}），拒绝删除：{key}")

    @contextlib.contextmanager
    def _exclusive_for_cleanup(self):
        """删除期间**独占持有** run.lock。

        * owner（`new()` 建的、已持有 `_lock_handle`）直接复用，不重复加锁
          （同进程内对同一文件重复加锁在 Windows 上会失败）；
        * 其它情况（`open()` / CLI / recover）必须**真拿到**锁：
          拿不到就说明有活跃持有者 → 拒绝清理（堵死绕过与 TOCTOU）。
        """
        existing = getattr(self, "_lock_handle", None)
        if existing is not None:
            yield                       # owner：已经在持有
            return
        handle = (self.dir / "run.lock").open("a+b")
        if not _acquire_os_lock(handle):
            handle.close()
            raise CleanupError(
                f"该 run 正被另一个进程持有（活跃 run：{self.run_uuid}）—— 拒绝清理"
            )
        try:
            yield
        finally:
            _release_os_lock(handle)
            try:
                handle.close()
            except OSError:
                pass

    #: run 自身的关键文件：**永不删除**（删了就没法审计/恢复）
    PROTECTED_SELF = ("manifest.json", "manifest.verified.json", "cleanup-report.json",
                      "heartbeat")

    def _check_path_allowed(self, raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            raise CleanupError(f"必须是绝对路径：{raw}")
        # 逐级检查：任何一级是链接都拒绝（防止经链接越界删除）
        for parent in [path, *path.parents]:
            if _is_link(parent):
                raise CleanupError(f"路径含链接，拒绝删除：{parent}")
        real = Path(os.path.realpath(path))
        if real == Path(os.path.realpath(self.dir)):
            raise CleanupError(f"拒绝删除 run 根目录自身：{real}")
        if real.name in self.PROTECTED_SELF:
            raise CleanupError(f"拒绝删除 run 关键文件：{real}")
        # 注意：不同盘符时 `commonpath` 会抛 ValueError（实测：C: 的临时目录
        # 对 D: 的项目目录），必须当作"越界"拒绝，而不是让异常冒成崩溃。
        inside = False
        for root in self.allowed_roots:
            try:
                if os.path.commonpath([str(real), str(root)]) == str(root):
                    inside = True
                    break
            except ValueError:
                continue          # 不同盘符 → 必然不在该根之下
        if not inside:
            raise CleanupError(f"越界，拒绝删除：{real}（允许根 {self.allowed_roots}）")
        return real

    def _assert_client_endpoint(self, qdrant_client) -> None:
        """交叉验证客户端**实际**端点与登记端点一致（规范化后**精确等值**）。

        实测真实字段是 ``qdrant_client._client.rest_uri``（例如
        ``http://127.0.0.1:56339``）。原实现取的 ``_client._base_url`` /
        外层 ``rest_uri`` / ``_host`` **全都不存在** → actual 恒为空 →
        `if ... and actual ...` 直接放行，交叉验证形同虚设。

        现在：**取不到就拒绝**（不猜），并且用 urlsplit 规范化后比较，
        **绝不**用 substring 包含判断。
        """
        actual = None
        inner = getattr(qdrant_client, "_client", None)
        if inner is not None:
            actual = getattr(inner, "rest_uri", None)
        if not actual:
            actual = getattr(qdrant_client, "rest_uri", None)
        if not actual:
            raise CleanupError(
                "无法确认传入 Qdrant 客户端的实际端点 —— 拒绝清理（安全优先，不猜）"
            )
        if not self.endpoint:
            raise CleanupError("本 run 未登记 endpoint —— 无法交叉验证，拒绝清理")
        if _norm_endpoint(str(actual)) != _norm_endpoint(self.endpoint):
            raise CleanupError(
                f"传入的 Qdrant 客户端端点与登记不一致：实际 {actual} ≠ 登记 {self.endpoint}"
            )

    def _assert_redis_allowed(self, redis_client) -> None:
        """Redis 只允许专用隔离 DB；**拒绝 DB0**（业务库）。"""
        db_index = None
        try:
            conn = getattr(redis_client, "connection_pool", None)
            kwargs = getattr(conn, "connection_kwargs", {}) or {}
            db_index = kwargs.get("db")
        except Exception:  # noqa: BLE001
            db_index = None
        if db_index is None:
            raise CleanupError("无法确认 Redis 客户端所用 DB —— 拒绝清理（安全优先）")
        if int(db_index) == FORBIDDEN_REDIS_DB:
            raise CleanupError(
                f"Redis DB{db_index} 是业务库（含现有队列消息）—— 拒绝清理"
            )

    def sweep_run_prefix(self, qdrant_client) -> list[str]:
        """按**本 run 独占前缀**清点并删除集合（真实流水线创建的也在内）。

        边界：**只**匹配 `self.prefix`（`kb-<uuid>-`），不是 `kb-*`、
        更不是 `kb-lt-*`；受保护名单与前缀校验照旧生效。
        这样"默认上传 / ensure_library_index"在会话内建出的集合
        （`kb-<uuid>-<lib>-g<n>`）也能被回收，不必逐个手工登记。
        """
        try:
            names = [c.name for c in qdrant_client.get_collections().collections]
        except Exception as exc:  # noqa: BLE001
            raise CleanupError(f"清点本 run 前缀失败：{exc}") from exc
        # **只**匹配本 run 独占前缀（不是 kb-*，更不是 kb-lt-*）
        candidates = [
            n for n in names
            if n.startswith(self.prefix)
            and n not in PROTECTED_NAMES
            and not n.startswith(PROTECTED_PREFIXES)
        ]

        # **先审计再删除**：把"即将删除的精确集合名 + 目标 endpoint"写进本 run
        # 清单。这样即使删除中途崩溃，也能从清单知道到底动过什么；
        # 绝不删除"清单里没有、审计里也查不到"的资源。
        if candidates:
            with self._lock:
                doc = self._read()
                for name in candidates:
                    if any(e["kind"] == "qdrant_collection" and e["key"] == name
                           for e in doc["entries"]):
                        continue
                    doc["entries"].append({
                        "kind": "qdrant_collection", "key": name, "status": "created",
                        "endpoint": self.endpoint, "identity": self.identity,
                        "declared_at": _now(), "source": "sweep_run_prefix",
                    })
                doc.setdefault("sweep_audit", []).append({
                    "at": _now(), "endpoint": self.endpoint,
                    "prefix": self.prefix, "discovered": candidates,
                })
                self._write(doc)          # ← 审计落盘**之后**才删

        removed: list[str] = []
        for name in candidates:
            qdrant_client.delete_collection(collection_name=name)
            removed.append(name)
            # 同步把清单里的登记标成 deleted（含下面补登记的 pending 项）
            with self._lock:
                doc = self._read()
                for entry in doc["entries"]:
                    if entry["kind"] == "qdrant_collection" and entry["key"] == name:
                        entry["status"] = "deleted"
                        entry["deleted_at"] = _now()
                        entry["deleted_by"] = "sweep_run_prefix"
                self._write(doc)
        return removed

    def reconcile_pending(self, qdrant_client) -> list[str]:
        """把"声明了但没 confirm"、实际却已建出来的集合**补登记后**删除。

        为什么必须做：进程可能死在 `declare` 与 `confirm` 之间 —— 资源已经真实
        创建，status 却还是 pending。原实现直接跳过 pending → **永久残留**。
        这里按本 run 独占前缀去端点上核对，确实存在的补登记为 created（**先写审计**）
        再删；不存在则标记为 missing（无害）。
        """
        doc = self._read()
        # 集合型 pending：按本 run 独占前缀核对
        pending = [
            e["key"] for e in doc["entries"]
            if e["kind"] == "qdrant_collection" and e["status"] == "pending"
            and e["key"].startswith(self.prefix)
            and self._entry_bound(e)
        ]
        # **文件/目录型 pending**：进程可能已建出目录但没来得及 confirm。
        # 只在本 run 授权范围内核对（真实包含 + 拒绝链接），绝不越界。
        pending_paths = [
            e["key"] for e in doc["entries"]
            if e["kind"] in ("file", "dir") and e["status"] == "pending"
            and self._entry_bound(e)
        ]
        if pending_paths:
            self._reconcile_pending_paths(pending_paths)
        if not pending:
            return []
        try:
            existing = {c.name for c in qdrant_client.get_collections().collections}
        except Exception as exc:  # noqa: BLE001
            raise CleanupError(f"协调 pending 资源失败（无法列出集合）：{exc}") from exc
        reconciled: list[str] = []
        for key in pending:
            with self._lock:
                doc = self._read()
                entry = next((e for e in doc["entries"]
                              if e["kind"] == "qdrant_collection" and e["key"] == key), None)
                if entry is None:
                    continue
                if key in existing:
                    # **先审计**：记录"确认它其实已存在"，再走删除
                    entry["status"] = "created"
                    entry["reconciled_at"] = _now()
                    entry["reconciled_reason"] = "pending 但端点已存在（进程死在 confirm 前）"
                    doc.setdefault("reconcile_audit", []).append(
                        {"at": _now(), "key": key, "endpoint": self.endpoint,
                         "verdict": "existed_despite_pending"}
                    )
                    self._write(doc)
                    reconciled.append(key)
                else:
                    entry["status"] = "missing"
                    entry["reconciled_at"] = _now()
                    self._write(doc)
        return reconciled

    @staticmethod
    def _deletable_mode(path: Path) -> int:
        """计算"让 owner 能进入并删除"的新权限位：**只加位、绝不清位**。

        跨平台注意（真实缺陷，Linux uid 10001 容器复现）：
          * **目录**必须保留/获得 owner 的 **x（执行/进入）位** —— 缺 x 时
            Linux 连枚举与删除都做不了，`rmtree` 直接 `PermissionError`；
          * **文件**owner 有 rw 即可；
          * 旧实现是 `chmod(S_IWRITE | S_IREAD)`（**赋值**，等于 0600），
            它把目录的执行位也清空了 —— 在 Windows 上碰巧能过，在 Linux 上必炸。
          因此这里改成 `原权限 | (0o700 / 0o600)`：其它权限位（组/其他/粘滞）
          **原样保留**，不做破坏性清零。
        """
        import stat as _stat

        # 只对**条目自身**取属性，不跟随符号链接（链接由调用方先行拒绝）
        mode = os.stat(path, follow_symlinks=False).st_mode
        perms = _stat.S_IMODE(mode)
        if _stat.S_ISDIR(mode):
            return perms | 0o700
        return perms | 0o600

    def _remove_tree_owned(self, target: Path) -> None:
        """删除**本 run 边界内**的目录树：先补可写/可进入位再删，拒绝链接。

        为什么需要：`storage.store_bytes` 按不可变语义写**只读**文件，
        Windows 上 `shutil.rmtree` 删只读文件会 `WinError 5`；Linux 上若目录丢 x 位，
        连进入都失败。这里只对**已通过真实包含校验**的 target 操作，
        绝不碰主 `media/raw`。补位后若仍失败就抛错 —— **不用** `ignore_errors`。
        """
        def _on_error(func, path, exc_info):  # noqa: ANN001
            # 兜底重试：再补一次位再试；仍失败就让它抛出去（保持失败可见）
            try:
                os.chmod(path, self._deletable_mode(Path(path)))
                func(path)
            except Exception:  # noqa: BLE001
                raise

        # 自底向上：先处理文件、再处理目录，顺序反了会因目录不可进入而失败
        for root, dirs, files in os.walk(target, topdown=False):
            for name in files + dirs:
                item = Path(root) / name
                if _is_link(item):
                    raise CleanupError(f"树下含链接，拒绝删除：{item}")
                try:
                    # **目录给 0o700、文件给 0o600**（只加位，不清位）
                    os.chmod(item, self._deletable_mode(item))
                except OSError as exc:
                    raise CleanupError(f"补可写位失败：{item}（{exc}）") from exc
        try:
            os.chmod(target, self._deletable_mode(target))
        except OSError as exc:
            raise CleanupError(f"补目录可写位失败：{target}（{exc}）") from exc
        shutil.rmtree(target, onerror=_on_error)   # 注意：**没有** ignore_errors

    def _reconcile_pending_paths(self, keys: list[str]) -> list[str]:
        """协调"已建出但没来得及 confirm"的文件/目录（本 run 范围内）。"""
        promoted: list[str] = []
        for raw in keys:
            try:
                # 复用同一套安全校验：必须绝对路径、逐级拒绝链接、真实包含在
                # allowed_roots（= 本 run 目录）之内。
                target = self._check_path_allowed(raw)
            except CleanupError:
                # 越界/链接 → 不提升（保持 pending，由报告显式暴露，不静默）
                continue
            exists = target.is_file() or target.is_dir()
            with self._lock:
                doc = self._read()
                entry = next((e for e in doc["entries"] if e["key"] == raw), None)
                if entry is None:
                    continue
                entry["reconciled_at"] = _now()
                if exists:
                    entry["status"] = "created"      # **先审计，后由 cleanup 删**
                    entry["reconciled_reason"] = (
                        "pending 但路径已存在（进程死在 confirm 前）"
                    )
                    doc.setdefault("reconcile_audit", []).append(
                        {"at": _now(), "key": raw, "kind": entry["kind"],
                         "verdict": "existed_despite_pending"}
                    )
                    promoted.append(raw)
                else:
                    entry["status"] = "missing"
                self._write(doc)
        return promoted

    def cleanup(self, *, qdrant_client=None, redis_client=None,
                sweep_run_prefix: bool = False) -> dict[str, Any]:
        """只删本次登记且已创建的资源；幂等；失败收集后统一报错。"""
        from qdrant_client import QdrantClient  # 延迟导入，便于无依赖场景

        with self._exclusive_for_cleanup():
            return self._cleanup_locked(
                QdrantClient, qdrant_client, redis_client, sweep_run_prefix
            )

    def _cleanup_locked(self, QdrantClient, qdrant_client, redis_client,
                        sweep_run_prefix) -> dict[str, Any]:
        """真正的清理实现（调用方已持有 run.lock）。"""
        doc = self._read()
        deleted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        # 需要 client 的两种情形：① 有已登记待删的集合；② 请求了前缀清点。
        # 原先只判断① → 会话里只登记 raw 时 client 恒为 None，
        # **前缀清点被静默跳过**，真实流水线集合留在端点上（实测泄漏）。
        # 注意：**pending 也要 client** —— 进程可能已 create 但死在 confirm 前，
        # 只看 created 会让这些集合被静默跳过并"报成功"（真实泄漏）。
        needs_client = (
            any(e["kind"] == "qdrant_collection" and e["status"] in ("created", "pending")
                for e in doc["entries"])
            or (sweep_run_prefix and bool(self.endpoint))
        )
        if qdrant_client is None and needs_client:
            qdrant_client = QdrantClient(url=self.endpoint, timeout=20)
        # **交叉验证**传入客户端的真实端点：防止"以为是隔离库、其实连了主实例"
        if qdrant_client is not None:
            self._assert_client_endpoint(qdrant_client)
        if redis_client is not None:
            self._assert_redis_allowed(redis_client)

        for entry in doc["entries"]:
            kind, key = entry["kind"], entry["key"]
            if entry["status"] == "deleted":
                skipped.append(f"{kind}:{key}（已删过）")
                continue
            if entry["status"] != "created":
                skipped.append(f"{kind}:{key}（未创建成功，无需清理）")
                continue
            # **绑定校验**：必须与本次 run 的 endpoint + identity 完全一致
            if not self._entry_bound(entry):
                failed.append(
                    f"{kind}:{key}（登记 endpoint/identity 与本次不一致，拒绝清理）"
                )
                continue
            try:
                if kind == "qdrant_collection":
                    self._check_collection_allowed(key)
                    # **幂等**：集合不存在就是"已经干净了"，不是失败
                    if qdrant_client.collection_exists(key):
                        qdrant_client.delete_collection(collection_name=key)
                elif kind in ("file", "dir"):
                    target = self._check_path_allowed(key)
                    if target.is_file():
                        # 单文件同样可能是只读（不可变原件）：**只加 owner 写位**
                        try:
                            os.chmod(target, self._deletable_mode(target))
                        except OSError as exc:
                            raise CleanupError(f"补可写位失败：{target}（{exc}）") from exc
                        target.unlink()
                    elif target.is_dir():
                        self._remove_tree_owned(target)
                    # 不存在 → 什么都不做（幂等）
                elif kind == "redis_key":
                    if redis_client is None:
                        # 未提供隔离客户端 → 明确失败（不静默、也不崩）
                        failed.append(f"redis_key:{key}（未提供隔离 Redis 客户端）")
                        continue
                    if not key.startswith(self.prefix):
                        raise CleanupError(
                            f"redis key 不属于本次 run 前缀，拒绝删除：{key}"
                        )
                    # 哈希字段要用 HDEL（DEL 删不掉字段）——登记里可带 field
                    field = entry.get("field")
                    if field:
                        redis_client.hdel(key, field)
                    else:
                        redis_client.delete(key)
            except Exception as exc:  # noqa: BLE001 - 收集后统一报告
                failed.append(f"{kind}:{key}（{type(exc).__name__}: {exc}）")
                continue
            # **成功即立刻落盘**：后面任何异常都不会丢掉"已删除"这个事实
            entry["status"] = "deleted"
            entry["deleted_at"] = _now()
            self._write(doc)
            deleted.append(f"{kind}:{key}")

        if qdrant_client is not None:
            # 先协调"pending 但实际已创建"的资源，再走正常清理。
            # 不再依赖 sweep_run_prefix —— 否则 recover() 会跳过它们并报成功。
            self.reconcile_pending(qdrant_client)
        if sweep_run_prefix and qdrant_client is None:
            # 静默跳过就是泄漏。要么给 client，要么明确失败。
            raise CleanupError(
                "请求了前缀清点但没有可用的 Qdrant 客户端（endpoint 缺失？）"
                " —— 拒绝静默跳过，否则真实集合会泄漏"
            )
        if sweep_run_prefix and qdrant_client is not None:
            # 真实流水线（默认上传 / ensure_library_index）建出的集合不一定逐个
            # 被 declare，这里按**本 run 独占前缀**兜底清点。
            swept = self.sweep_run_prefix(qdrant_client)
            for name in swept:
                deleted.append(f"qdrant_collection:{name}（前缀清点）")

        # 保留**小型**审计报告，不保留大数据
        report = {
            "run_uuid": self.run_uuid, "endpoint": self.endpoint,
            "identity": self.identity, "deleted": deleted,
            "skipped": skipped, "failed": failed, "at": _now(),
        }
        _atomic_write(self.dir / "cleanup-report.json",
                      json.dumps(report, ensure_ascii=False, indent=2))
        if failed:
            raise CleanupError("清理失败：\n  - " + "\n  - ".join(failed))
        return report


# --------------------------------------------------------------- 恢复 ----
def recover(
    run_uuid: str,
    *,
    endpoint: str | None = None,
    identity: str | None = None,
    runs_dir: Path | None = None,
    force: bool = False,
    qdrant_client=None,
) -> dict[str, Any]:
    """遗留 run 的显式恢复清理。

    **必须**确认没有活跃 run（心跳过期）才允许动；`--force` 需人工明确指定。
    """
    # **只读打开**：读清单不能刷新心跳（否则永远判"存活"）
    res = RunResources.open(run_uuid, runs_dir=runs_dir)
    if endpoint and endpoint != res.endpoint:
        raise CleanupError(f"传入 endpoint 与清单登记不一致：{endpoint} ≠ {res.endpoint}")
    if identity and identity != res.identity:
        raise CleanupError(f"传入 identity 与清单登记不一致：{identity} ≠ {res.identity}")

    # **服务 PID 身份验证**：清单里若记了 runner 启动的服务 PID、且它仍存活，
    # 就说明"服务还活着、可能正在写本 run 的资源" —— 即使父锁已释放也不能清。
    # 只凭"父锁释放"不能认定服务/worker 也死了（审查指出）。
    # `--force` **也不绕过**：活着就无法保证不在写。
    meta_svc = res.get_meta()
    svc_pid = meta_svc.get("service_pid")
    if svc_pid:
        # **只读**探活；绝不 os.kill（Windows 上 os.kill(pid,0) 会 TerminateProcess）
        pid_alive = pid_is_alive_readonly(svc_pid)
        # PID 复用防护：创建时间令牌不一致 → 那不是我们那个进程 → 保守拒绝清理
        recorded = meta_svc.get("service_pid_start")
        if pid_alive and recorded:
            current = pid_start_token(int(svc_pid))
            if current is not None and abs(float(current) - float(recorded)) > 0.001:
                raise CleanupError(
                    f"PID {svc_pid} 已被复用（创建时间与登记不符）—— 拒绝清理"
                    "（保守处理：无法确认它是不是我们的服务）"
                )
        if pid_alive:
            raise CleanupError(
                f"清单记录的服务 PID {svc_pid} 仍存活 —— 拒绝清理"
                "（服务可能仍在写入本 run 的资源；请先停止它，--force 也不绕过）"
            )

    alive = res.is_alive()
    lock_free = not alive
    if alive and not lock_free:
        # **即使 --force 也拒绝**：锁还被持有，说明 run 真的在跑
        raise CleanupError(
            "该 run 仍持有跨进程锁（确实在运行）—— 拒绝清理（--force 也不能绕过）"
        )
    if alive and force:
        raise CleanupError("该 run 仍存活 —— 拒绝清理")
    if not alive and force:
        # 锁空闲但调用方仍要 force：允许，并记录原因
        pass
    # **必须开 sweep**：recover 面对的正是"进程死了、可能有 pending 残留"的现场；
    # 只按 created 清理会漏掉"已建但没来得及 confirm"的集合。
    # sweep 只匹配本 run 独占前缀 `kb-<uuid>-`（不是 kb-*），且会先写审计。
    report = res.cleanup(qdrant_client=qdrant_client,
                         sweep_run_prefix=bool(res.endpoint))
    # 报告不得把未清的 pending 说成成功：协调/清理后仍存在的条目已在 failed 里，
    # 这里再兜一层——若清单里仍有 pending 且 endpoint 有值，就明确标注。
    if res.endpoint:
        doc = res._read()
        leftover = [
            e["key"] for e in doc["entries"]
            if e["kind"] == "qdrant_collection" and e["status"] == "pending"
        ]
        if leftover:
            report.setdefault("failed", []).extend(
                f"pending 未能确认/回收：{k}" for k in leftover
            )
    return report


# ---------------------------------------------------------------- CLI ----
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 run 清理测试资源（精确匹配，禁止模糊扫删）")
    parser.add_argument("action", choices=["list", "cleanup", "recover", "recover-all"],
                        help="list=列出遗留 run（**只读，不扫删历史**）；"
                             "cleanup=清理指定 run；recover[-all]=恢复清理")
    parser.add_argument("--uuid", default="", help="run_uuid")
    parser.add_argument("--force", action="store_true", help="忽略心跳（仅人工确认后使用）")
    args = parser.parse_args(argv)

    try:
        if args.action == "list":
            if not RUNS_DIR.exists():
                print("没有测试 run 记录")
                return 0
            total_left = 0
            for d in sorted(RUNS_DIR.iterdir()):
                doc = d / "manifest.json"
                if not (d.is_dir() and doc.exists()):
                    continue
                entries = json.loads(doc.read_text(encoding="utf-8"))["entries"]
                left = [e for e in entries if e["status"] == "created"]
                total_left += len(left)
                try:
                    alive = RunResources.open(d.name, runs_dir=RUNS_DIR).is_alive()
                except CleanupError:
                    alive = False   # 目录名不合规（不是我们生成的 run）→ 不当作 run
                print(f"  {d.name}: 存活={alive} 待清理={len(left)}")
                for e in left[:10]:
                    print(f"      - {e['kind']}:{e['key']}")
            # 只报告；**绝不**在此处删除任何东西（尤其 kb-lt-* 历史集合）
            print(f"  合计待清理 {total_left} 项（本命令只列不删）")
            return 0
        if args.action == "cleanup":
            if not args.uuid:
                raise CleanupError("cleanup 需要 --uuid")
            # 只读打开（不刷新心跳），再清理
            res = RunResources.open(args.uuid)
            print(json.dumps(res.cleanup(), ensure_ascii=False, indent=2))
            return 0
        if args.action == "recover":
            if not args.uuid:
                raise CleanupError("recover 需要 --uuid")
            print(json.dumps(recover(args.uuid, force=args.force), ensure_ascii=False, indent=2))
            return 0
        # recover-all：逐个恢复，汇总失败
        failures = []
        if RUNS_DIR.exists():
            for d in sorted(RUNS_DIR.iterdir()):
                if not (d.is_dir() and (d / "manifest.json").exists()):
                    continue
                try:
                    recover(d.name, force=args.force)
                except CleanupError as exc:
                    failures.append(f"{d.name}: {exc}")
        if failures:
            print("部分 run 未能恢复清理：", file=sys.stderr)
            for item in failures:
                print("  - " + item, file=sys.stderr)
            return 1
        return 0
    except CleanupError as exc:
        print(f"清理失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
