"""独占测试运行器：把一次压测的 **DB / 服务 / raw / 向量集合** 全生命周期收尾。

为什么需要它（前几轮遗留的最后一个缺口）：
  `load_probe` 的 finally 只能清"本 run 的向量集合 + 本地目录" ——
  它管不到**服务端数据库**，也管不到"服务进程自己写下的只读原件"。
  于是"压测跑完"并不等于"干净"，真实 DB 会残留。

本运行器**拥有**下列全部资源，并在 `finally` 里逐项回收（失败即非零 + 报告残留）：
  1) 独占 run 目录 `artifacts/test-runs/<run_id>/{raw,vector_data}`；
  2) 独占 PG 库 `<db_name>`（**只**允许 `kb_loadtest_` 前缀，Django 不再建/销它）；
  3) 独占 Redis DB（默认 15；**拒绝** 0/1 业务库）；
  4) 本 run 独占向量前缀 `kb-<run_id>`；
  5) 它自己起的服务进程（停 → wait，然后才清理）。

安全边界：
  * 默认 `--dry-run`（**只打印计划，不做任何变更**）；真正执行要 `--execute`；
  * 拒绝指向主实例（`56333`）、业务 DB、业务 Redis DB；
  * 只删自己创建的那个库（名字必须匹配 `kb_loadtest_<suffix>`）；
  * 不碰共享 content-addressed 原件（主 `media/raw` 只读参照，永不修改）。

用法：
    python scripts/loadtest_runner.py --dry-run                     # 看计划
    python scripts/loadtest_runner.py --execute --files 5 --seconds 10
    python scripts/loadtest_runner.py --execute --skip-load         # 只验证生命周期
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
RUNS_DIR = ROOT / "artifacts" / "test-runs"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$|^[0-9a-f]{32}$")
DB_NAME_RE = re.compile(r"^kb_loadtest_[0-9a-f]{8}$")
#: 业务 Redis DB：绝不允许作为压测 broker
BUSINESS_REDIS_DBS = {"0", "1"}
#: 主实例端点特征：出现即拒绝
MAIN_INSTANCE_HINT = "56333"
TEST_PORT = "8011"
PG_CONTAINER = "knowledgebase-postgres"
PG_USER = "kb"


class RunnerError(RuntimeError):
    """致命错误：必须回滚并显式报告。"""


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


class LoadTestRun:
    """一次压测的全部资源与回收逻辑。"""

    def __init__(self, run_id: str, broker_db: str, execute: bool) -> None:
        if not RUN_ID_RE.match(run_id):
            raise RunnerError(f"run_id 必须是 12/32 位小写 hex：{run_id!r}")
        if broker_db in BUSINESS_REDIS_DBS:
            raise RunnerError(f"broker Redis DB 不能是业务库：{broker_db}")
        self.run_id = run_id
        self.broker_db = broker_db
        self.execute = execute
        self.suffix = secrets.token_hex(4)
        self.db_name = f"kb_loadtest_{self.suffix}"
        if not DB_NAME_RE.match(self.db_name):
            raise RunnerError(f"内部错误：库名不符合受控前缀：{self.db_name}")

        self.run_dir = RUNS_DIR / run_id
        self.raw_dir = self.run_dir / "raw"
        self.vector_dir = self.run_dir / "vector_data"
        self.endpoint = os.environ.get(
            "KB_TEST_QDRANT_URL", "http://127.0.0.1:56339"
        ).strip()
        if MAIN_INSTANCE_HINT in self.endpoint:
            raise RunnerError(f"拒绝：端点 {self.endpoint} 指向主实例")
        self.qdrant_prefix = f"kb-{run_id}"

        self.service: subprocess.Popen | None = None
        self.created_db = False
        #: 压测账号口令：只经 env 传递，**不打印、不落盘**
        self.load_user = f"loadtest_{self.suffix}"
        self.load_password = secrets.token_urlsafe(24)
        self.library_code = "load-test-main"   # 必须 load-test- 前缀（load_probe 会校验）
        self.residual: list[str] = []
        self.done: list[str] = []

        # **以 owner 身份持有本 run 的锁**（④）：
        #   * 同一 run_id 的第二个 runner 会因锁被占而**拒绝启动**，
        #     不会出现"两个 runner 互删对方资源"；
        #   * RunResources.prefix 是 `kb-<run_id>-`（**带尾连字符**），
        #     正好满足⑤要求的精确前缀；
        #   * 复用共享实现（审计/受保护名单/链接拒绝/只读位），不另写扫删。
        sys.path.insert(0, str(SCRIPTS))
        import run_resources as _rr

        # 复用**已存在的 run 目录**必须拒绝：那可能是上次跑到一半的现场，
        # 直接复用会覆盖审计，并可能删到它的资源。
        if (self.run_dir / "manifest.json").exists():
            raise RunnerError(
                f"run_id 已存在现场（{self.run_dir}），拒绝复用；"
                "请换一个 run_id 或先用 run_resources.py recover 处理它"
            )
        # **只有 execute 才真的建 run**：否则 dry-run 会建出目录/清单/锁，
        # 那就不是"零变更"了（我第一版就是这样，属于自相矛盾）。
        self.run_resources = None
        if self.execute:
            self.run_resources = _rr.RunResources.new(
                endpoint=self.endpoint, identity="loadtest-runner",
                run_uuid=self.run_id, runs_dir=RUNS_DIR,
            )

    # ------------------------------------------------------------ 计划 ----
    def load_env(self) -> dict:
        """给 `load_probe` **客户端**用的环境：额外带上传所需的账号/库/写权限。

        凭据只走环境变量（阻断 8：不打印、不写文件）。
        """
        env = self.env()
        env.update(
            {
                "KB_LOAD_BASE": f"http://127.0.0.1:{TEST_PORT}",
                "KB_LOAD_USER": self.load_user,
                "KB_LOAD_PASSWORD": self.load_password,
                "KB_LOAD_LIBRARY": self.library_code,
                "KB_LOAD_ALLOW_WRITE": "1",   # 允许 upload 模式真正写库
                # **受管模式**：告诉 load_probe 不要自建/自清 run（资源归本 runner）
                "KB_LOAD_MANAGED": "1",
                "KB_LOAD_RUN_PREFIX": f"kb-{self.run_id}",
            }
        )
        return env

    def env(self) -> dict:
        """给被它拥有的服务/客户端用的整套隔离环境变量（**不含凭据**）。"""
        env = dict(os.environ)
        env.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.loadtest_settings",
                "KB_LOADTEST_RUN_ID": self.run_id,
                "KB_LOADTEST_DB_NAME": self.db_name,
                "KB_LOADTEST_QDRANT_PREFIX": self.qdrant_prefix,
                "KB_LOADTEST_QDRANT_URL": self.endpoint,
                "KB_LOADTEST_BROKER_DB": self.broker_db,
                "KB_LOADTEST_RAW_STORE_DIR": str(self.raw_dir),
                "KB_LOADTEST_VECTOR_DATA_DIR": str(self.vector_dir),
                "RAW_STORE_DIR": str(self.raw_dir),
                "VECTOR_DATA_DIR": str(self.vector_dir),
                "KB_QDRANT_URL": self.endpoint,
                "KB_LOAD_RUN_ID": self.run_id,
            }
        )
        return env

    def describe(self) -> None:
        step("本次 run 的资源计划（--dry-run 不做任何变更）")
        say(f"run_id      : {self.run_id}")
        say(f"向量前缀    : {self.qdrant_prefix}（只删这个前缀的集合）")
        say(f"隔离端点    : {self.endpoint}")
        say(f"独占 PG 库  : {self.db_name}（只允许 kb_loadtest_ 前缀）")
        say(f"独占 Redis  : DB{self.broker_db}（业务库 {sorted(BUSINESS_REDIS_DBS)} 已拒绝）")
        say(f"raw 目录    : {self.raw_dir}")
        say(f"向量目录    : {self.vector_dir}")
        say(f"服务端口    : 127.0.0.1:{TEST_PORT}（由本运行器启动并拥有）")
        say("收尾顺序    : 停服务 → wait → 清本 run 向量 → 清 raw/vector → DROP 本 run 库 → 写报告")

    # -------------------------------------------------------- 建/启动 ----
    def create_db(self) -> None:
        step(f"创建独占库 {self.db_name}")
        if not self.execute:
            say("[dry-run] 跳过")
            return
        # 只建自己命名空间内的库；已存在则拒绝（避免误用别人的库）
        exists = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname='{self.db_name}'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if exists.stdout.strip() == "1":
            raise RunnerError(f"库已存在，拒绝复用：{self.db_name}")
        made = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-c",
             f'CREATE DATABASE "{self.db_name}"'],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if made.returncode != 0:
            raise RunnerError(f"建库失败：{made.stderr[:200]}")
        self.created_db = True
        say("已创建")
        migrate = subprocess.run(
            [str(PYTHON), "manage.py", "migrate", "--no-input"],
            cwd=str(ROOT), env=self.env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if migrate.returncode != 0:
            raise RunnerError(f"迁移失败：{migrate.stderr[-300:]}")
        say("迁移完成")

    def seed_account(self) -> None:
        """在**本 run 的独占库**里种子测试账号与 `load-test-` 库。

        独占库是空的：不种子就"上传必失败"（登录失败 / 目标库不存在）。
        口令只经环境变量传给客户端，这里**不打印**。
        """
        step("种子测试账号与测试库（独占库）")
        if not self.execute:
            say("[dry-run] 跳过")
            return
        snippet = (
            "from django.contrib.auth import get_user_model\n"
            "from django.utils import timezone\n"
            "from apps.knowledge.models import KnowledgeLibrary\n"
            "import os\n"
            "U = get_user_model()\n"
            "u, _ = U.objects.get_or_create(username=os.environ['KB_SEED_USER'])\n"
            "u.set_password(os.environ['KB_SEED_PASSWORD'])\n"
            "u.is_staff = True\n"
            "u.is_superuser = True\n"
            "u.save()\n"
            "lib, _ = KnowledgeLibrary.objects.get_or_create(\n"
            "    code=os.environ['KB_SEED_LIBRARY'],\n"
            "    defaults={'name': '压测库（独占）'},\n"
            ")\n"
            "print('SEED_OK', u.username, lib.code)\n"
        )
        env = self.env()
        # 口令只经 env 传入子进程（不出现在命令行里）
        env["KB_SEED_USER"] = self.load_user
        env["KB_SEED_PASSWORD"] = self.load_password
        env["KB_SEED_LIBRARY"] = self.library_code
        proc = subprocess.run(
            [str(PYTHON), "manage.py", "shell", "-c", snippet],
            cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        if proc.returncode != 0 or "SEED_OK" not in proc.stdout:
            raise RunnerError(f"种子失败：{proc.stderr[-300:]}")
        # **只报成功与否，不回声口令**
        say(f"已种子账号 {self.load_user} 与库 {self.library_code}（口令经 env 传递，不打印）")
        self.done.append("seeded")

    def start_service(self) -> None:
        step(f"启动本运行器拥有的服务（127.0.0.1:{TEST_PORT}）")
        if not self.execute:
            say("[dry-run] 跳过")
            return

        # ① 端口必须空闲：被占用说明有别人的服务在跑，绝不能把它当成自己的
        import socket

        with socket.socket() as probe_sock:
            probe_sock.settimeout(1.0)
            if probe_sock.connect_ex(("127.0.0.1", int(TEST_PORT))) == 0:
                raise RunnerError(
                    f"端口 {TEST_PORT} 已被占用 —— 拒绝启动（不能把别人的服务当成自己的）"
                )

        # **先建目录再开日志**：run_dir 可能还不存在（第一次跑），
        # 直接 open 会 FileNotFoundError —— 这是会让 --execute 必失败的静态阻断。
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = (self.run_dir / "service.log").open("w", encoding="utf-8")
        self.service = subprocess.Popen(
            [str(PYTHON), "manage.py", "runserver", f"127.0.0.1:{TEST_PORT}", "--noreload"],
            cwd=str(ROOT), env=self.env(), stdout=log, stderr=subprocess.STDOUT,
        )

        # ②③ 轮询：健康 + **登录后**核对 guide 身份 + 进程存活
        import json as _json

        sys.path.insert(0, str(SCRIPTS))
        import load_probe as _lp

        ready_url = f"http://127.0.0.1:{TEST_PORT}/api/v1/health/ready/"
        deadline = time.time() + 90
        last = ""
        while time.time() < deadline:
            if self.service.poll() is not None:
                raise RunnerError(
                    f"服务启动即退出（码 {self.service.returncode}），见 service.log"
                )
            try:
                # 登录用**本 run 种子账号**；凭据只在内存
                session = _lp.Session(f"http://127.0.0.1:{TEST_PORT}")
                session.login(self.load_user, self.load_password)
                status, guide = session.request("GET", "/api/v1/planner/guide/")
                if status != 200:
                    last = f"guide 状态 {status}"
                    time.sleep(1)
                    continue
            except Exception as exc:  # noqa: BLE001
                last = f"login/guide: {exc}"
                time.sleep(1)
                continue

            env = (guide.get("environment") or guide.get("data", {}).get("environment")
                   or guide)
            prefix = str(env.get("qdrant_collection_prefix") or "")
            if prefix != self.qdrant_prefix:
                # **关键**：端口上那个服务的身份不是本 run → 拒绝认领
                raise RunnerError(
                    f"{TEST_PORT} 上的服务自述前缀 {prefix!r} 与本 run "
                    f"{self.qdrant_prefix!r} 不一致 —— 拒绝把它当成自己的服务"
                )
            # 隔离口径三连：任一不是 True 都说明连错了服务
            for key in ("model_calls_disabled", "load_test_mode", "dispatch_disabled"):
                if env.get(key) is not True:
                    raise RunnerError(
                        f"目标服务 {key}={env.get(key)!r}（应为 True）—— 拒绝在非隔离服务上压测"
                    )
            if self.service.poll() is not None:
                raise RunnerError("服务在就绪校验期间退出")
            # ① 把**服务 PID + 创建时间令牌 + 独占库名**记进 run 清单：
            # recover 会据此做**只读**身份校验（不能只凭父锁释放就认定服务已死）。
            # 令牌用于防 PID 复用（系统会复用 PID，只比 PID 不足以确认身份）。
            import run_resources as _rr2

            self.run_resources.set_meta(
                service_pid=self.service.pid,
                service_pid_start=_rr2.pid_start_token(self.service.pid),
                db_name=self.db_name,
                endpoint=self.endpoint,
                qdrant_prefix=self.qdrant_prefix,
            )
            say(f"服务就绪且身份已核对（PID {self.service.pid}，前缀 {prefix}）")
            return
        raise RunnerError(f"服务未在 90s 内就绪（最后错误 {last}）")

    def run_load_probe(self, files: int, seconds: int) -> None:
        step("执行 load_probe（本 run）")
        if not self.execute:
            say("[dry-run] 跳过")
            return
        cmd = [
            str(PYTHON), str(SCRIPTS / "load_probe.py"), "upload",
            "--concurrency", "4", "--files", str(files), "--seconds", str(seconds),
            "--run-id", self.run_id, "--base", f"http://127.0.0.1:{TEST_PORT}",
        ]
        proc = subprocess.run(
            cmd, cwd=str(ROOT), env=self.load_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(120, seconds * 6),
        )
        say(f"load_probe 退出码 = {proc.returncode}")
        if proc.returncode != 0:
            self.residual.append(f"load_probe 非零退出（{proc.returncode}）")
        # 加固：任何一路输出都可能是 None（解码失败/被吞），不能直接相加
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "load-probe.log").write_text(
            (proc.stdout or "") + (proc.stderr or ""), encoding="utf-8"
        )
        # 把关键摘要回显到控制台（便于立即判断失败原因，不必翻日志）
        tail = [ln for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if ln.strip()]
        for line in tail[-6:]:
            say("  | " + line[:160])

    # ------------------------------------------------------------ 收尾 ----
    def stop_service(self) -> None:
        if self.service is None:
            return
        step("停服务并 wait（先停后清）")
        if self.service.poll() is None:
            self.service.terminate()
            try:
                self.service.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.service.kill()
                self.service.wait(timeout=10)
        say(f"服务已退出（码 {self.service.returncode}）")
        self.done.append("service_stopped")

    def clean_qdrant(self) -> None:
        step(f"清理本 run 向量前缀 {self.qdrant_prefix}")
        if not self.execute:
            say("[dry-run] 跳过")
            return
        # **复用已验收的共用清理器**，不再自己写一套无审计的扫删：
        #   * 精确前缀 `kb-<run_id>-`（**带尾连字符**）——
        #     少写这个连字符会误中"另一个更长 UUID"的集合（如 kb-<id>extra-…）；
        #   * 它带受保护名单、先审计后删除、并返回删除清单。
        if self.run_resources is None:
            self.residual.append("缺少 run 资源登记（未以 owner 身份启动）—— 拒绝清理")
            return
        try:
            removed = self.run_resources.sweep_run_prefix(self.qdrant_client())
        except Exception as exc:  # noqa: BLE001
            self.residual.append(f"向量清理失败：{type(exc).__name__}: {exc}")
            return
        say(f"已删 {len(removed)} 个（精确前缀 {self.run_resources.prefix}）")
        for name in removed:
            self.done.append(f"collection:{name}")

    def qdrant_client(self):
        from qdrant_client import QdrantClient

        return QdrantClient(url=self.endpoint, timeout=20)

    def clean_dirs(self) -> None:
        step("清理本 run 的 raw / vector_data")
        if not self.execute:
            say("[dry-run] 跳过")
            return
        # **复用共用清理器**：它内部会 (a) 拒绝链接（含祖先/子项）、
        # (b) 校验真实绝对包含关系（allowed_roots = run 目录）、
        # (c) 清只读位后删除、(d) 失败即抛（不 ignore_errors）。
        # 另加一道：run_dir 自身必须真实位于 artifacts/test-runs 之下。
        runs_root = Path(os.path.realpath(RUNS_DIR))
        real_run = Path(os.path.realpath(self.run_dir))
        if os.path.commonpath([str(real_run), str(runs_root)]) != str(runs_root):
            self.residual.append(f"run 目录越界，拒绝清理：{real_run}")
            return
        for target in (self.raw_dir, self.vector_dir):
            if not target.exists():
                continue
            try:
                if self.run_resources is None:
                    self.residual.append("缺少 run 资源登记 —— 拒绝清理目录")
                    raise RunnerError("缺少 run 资源登记")
                self.run_resources._remove_tree_owned(Path(os.path.realpath(target)))
                say(f"已删 {target.name}")
                self.done.append(f"dir:{target.name}")
            except Exception as exc:  # noqa: BLE001
                self.residual.append(f"{target.name} 清理失败：{type(exc).__name__}: {exc}")

    def drop_db(self) -> None:
        step(f"回收独占库 {self.db_name}")
        if not self.execute:
            say("[dry-run] 跳过")
            return
        if not self.created_db:
            say("本运行器未创建过库，拒绝 DROP（防止误删别人的库）")
            return
        if not DB_NAME_RE.match(self.db_name):
            self.residual.append(f"库名不在受控命名空间，拒绝 DROP：{self.db_name}")
            return
        # 先断开本 run 服务的连接，再 DROP
        subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-c",
             f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             f"WHERE datname='{self.db_name}'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        drop = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-c",
             f'DROP DATABASE IF EXISTS "{self.db_name}"'],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if drop.returncode != 0:
            self.residual.append(f"DROP DATABASE 失败：{drop.stderr[:200]}")
        else:
            say("已回收")
            self.done.append("db_dropped")

    def write_report(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "run_id": self.run_id,
            "endpoint": self.endpoint,
            "qdrant_prefix": self.qdrant_prefix,
            "db_name": self.db_name,
            "broker_db": self.broker_db,
            "execute": self.execute,
            "done": self.done,
            "residual": self.residual,
            "at": time.time(),
        }
        (self.run_dir / "runner-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        say(f"报告：{(self.run_dir / 'runner-report.json').relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独占测试运行器（默认 dry-run）")
    parser.add_argument("--run-id", default=secrets.token_hex(6))
    parser.add_argument("--broker-db", default="15")
    parser.add_argument("--files", type=int, default=5)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--skip-load", action="store_true", help="只验证生命周期，不发压")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印资源计划，零变更（**默认行为**；加 --execute 才真正执行）",
    )
    parser.add_argument("--execute", action="store_true", help="真正创建/启动/清理")
    args = parser.parse_args(argv)

    # 默认就是 dry-run；显式给 --dry-run 也走同一条路（文档与用法一致）。
    if args.dry_run:
        args.execute = False

    run = LoadTestRun(args.run_id, args.broker_db, args.execute)
    run.describe()
    if not args.execute:
        say("\n[dry-run] 未做任何变更；确认后加 --execute")
        return 0

    try:
        run.create_db()
        run.seed_account()
        run.start_service()
        if not args.skip_load:
            run.run_load_probe(args.files, args.seconds)
    finally:
        # **无论成功/失败/异常/Ctrl+C 都收尾**。
        # 关键：各项**独立**收集失败 —— 以前放在同一个 try 里，
        # 向量清理一失败就跳过 raw 与 DB，导致"全残留"。
        try:
            run.stop_service()
        except Exception as exc:  # noqa: BLE001
            run.residual.append(f"停服务失败：{type(exc).__name__}: {exc}")

        # 服务若仍在运行，就**拒绝删除仍在被写入的资源**并明确保留（不硬删）。
        if run.service is not None and run.service.poll() is None:
            run.residual.append(
                "服务仍在运行 —— 拒绝删除仍在写入的向量/raw/DB，相关资源已保留（请人工确认）"
            )
        else:
            for name, action in (
                ("向量", run.clean_qdrant),
                ("目录", run.clean_dirs),
                ("数据库", run.drop_db),
            ):
                try:
                    action()
                except Exception as exc:  # noqa: BLE001
                    run.residual.append(f"{name}收尾异常：{type(exc).__name__}: {exc}")
        run.write_report()

    if run.residual:
        print("\n收尾未完成，残留项：", file=sys.stderr)
        for item in run.residual:
            print("  - " + item, file=sys.stderr)
        return 1
    print(f"\n闭合完成：服务已停、本 run 向量已清、raw/vector 已清、库 {run.db_name} 已回收")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
