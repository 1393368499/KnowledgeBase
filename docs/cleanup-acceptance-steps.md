# 自动清理：完整可执行验收步骤

本文件是**可直接照抄执行**的验收步骤（Windows + 项目 `.venv`）。
每一步都给出**期望输出**；不符即为不通过。

> 安全前提（每一步都成立）：
> * 只对**隔离端点**（`56339`）建/删集合；**主实例 `56333` 全程只读**；
> * 删除**只按本 run 独占前缀** `kb-<run_uuid>-` 精确匹配，
>   **绝不**扫 `kb-*` / `kb-lt-*`；
> * 历史 191 个集合（含 125 个 `kb-lt-*`、约 42 GiB）**不在**自动删除范围内；
> * 不碰 Redis DB0、不碰共享 content-addressed 原件、不动 AS711 任何服务。

---

## 0. 环境准备

```powershell
cd D:\AProjectScript\KnowledgeBase
$env:KB_PYTEST_DB     = 'sqlite'
$env:KB_TEST_QDRANT_URL = 'http://127.0.0.1:56339'   # 隔离端点；不设则退化为 embedded
```

隔离端点必须只剩它自己的探针集合：

```powershell
.\.venv\Scripts\python.exe -c "from qdrant_client import QdrantClient as C; print([x.name for x in C(url='http://127.0.0.1:56339',timeout=20).get_collections().collections])"
# 期望：['codex-runtime-probe-20261015']
```

## 1. 主实例只读保护基线（写一次即锁，永不覆盖）

```powershell
.\.venv\Scripts\python.exe scripts\cleanup_probe.py --endpoint http://127.0.0.1:56333 --protect
# 期望：现有 191 个集合，media/raw 271 个文件
#       已存在 → 「主实例保护基线已存在（191 集合 / 271 raw 文件），**不覆盖**」
```

## 2. 单元 + 跨进程：清理器行为

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_run_cleanup.py tests\test_run_cleanup_crossproc.py tests\test_run_cleanup_readonly.py -p no:randomly -q
# 期望：全通过（含 1 个符号链接用例在本机 skip）
```

覆盖点：精确登记匹配、幂等、越界/链接拒绝、`kb-1-g1` 哨兵保护、
**子进程持锁时 cleanup/recover 一律拒绝**、**两个恢复者并发互斥**、
**只读不可变原件可被清理**、清理失败非零且不 `ignore_errors`。

## 3. 真实 pytest 入口：会话 finally 自动清理

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_run_cleanup_e2e.py tests\test_run_cleanup_pipeline.py -p no:randomly -q
# 期望：全通过；用例**故意不删**集合，删除由 conftest 会话 finally 完成
```

随后独立进程复核（**必须**回到 1 个集合）：

```powershell
.\.venv\Scripts\python.exe scripts\cleanup_probe.py --endpoint http://127.0.0.1:56339
# 期望：真实闭环通过：原有 1 个集合全部保留（含 codex-runtime-probe-20261015 …），
#       本 run 集合已删除，计数回到 1
```

## 4. 真实"故意失败仍清理"演练（退码必须恰为 1）

```powershell
.\.venv\Scripts\python.exe scripts\cleanup_failure_drill.py --endpoint http://127.0.0.1:56339; "drill exit = $LASTEXITCODE"
# 期望：
#   演练前集合（1）：['codex-runtime-probe-20261015']
#   子进程退码 = 1
#   marker：{'collection': 'kb-<uuid>-1-g1', ...}      ← 证明确实创建成功
#   演练后集合（1）：['codex-runtime-probe-20261015']    ← 已清，无泄漏
#   演练通过：... cleanup-report.failed=[]、raw/vector_data 已清（自身退码 0）
```

## 5. 强杀恢复（遗留 run）

```powershell
.\.venv\Scripts\python.exe scripts\run_resources.py list          # 只列不删
.\.venv\Scripts\python.exe scripts\run_resources.py recover-all    # 仅对"锁已释放"的 run
# 期望：活跃 run（锁被持有）一律拒绝；已死 run 的 raw/集合被回收
```

## 6. 就绪初始化复验（readiness）

```powershell
.\.venv\Scripts\python.exe -m pytest "tests/test_api_basics.py::test_health_is_open" -p no:randomly -q
# 期望：通过（VECTOR_DATA_DIR 在 settings 阶段已预建，health 不被 mock、不接受 503）
```

## 7. 三方状态终检

```powershell
"主实例 = " + (Invoke-RestMethod http://127.0.0.1:56333/collections -TimeoutSec 5).result.collections.Count + " / 191"
"media/raw = " + (Get-ChildItem media\raw -Recurse -File).Count + " / 271"
"隔离端点 = " + (Invoke-RestMethod http://127.0.0.1:56339/collections -TimeoutSec 5).result.collections.Count + " / 1"
```

---

## 验收矩阵

| 场景 | 命令 | 状态 |
|---|---|---|
| 正常结束自动清理 | 步骤 3 | ✅ 已通过 |
| 用例故意失败仍清理 | 步骤 4 | ✅ 已通过（退码恰为 1 + marker 佐证） |
| 重复清理幂等 | 步骤 2 | ✅ 已通过 |
| 同 endpoint 两 run 互不影响 | 步骤 2 | ✅ 已通过 |
| `kb-1-g1` 哨兵不可删 | 步骤 2 | ✅ 已通过 |
| 越界 / junction / 未登记集合拒绝 | 步骤 2 | ✅ 已通过 |
| 遗留 run 恢复 | 步骤 5 | ✅ 已通过（心跳/锁判活） |
| 清理失败非零 + 显式报告 | 步骤 2 | ✅ 已通过 |
| 活跃 run 保护（含 CLI 绕过） | 步骤 2 | ✅ 已通过（子进程持锁负例） |
| 只读不可变原件清理 | 步骤 2 | ✅ 已通过 |
| 读就绪初始化 | 步骤 6 | ✅ 已通过 |
| `load_probe` 全模式 try/finally | — | ⚠️ **未完成** |
| `test_real_worker` 真实 worker 退出后清理 | — | ⚠️ **未验证**（本机无 broker/worker，全 skip） |

## 保留与不保留

* **保留**：`artifacts/test-runs/<run_uuid>/` 下的 `manifest.json`、
  `cleanup-report.json`、`heartbeat`、`run.lock`（小型审计，便于复核与恢复）；
* **不保留**：`raw/`、`vector_data/`（大数据），以及本 run 前缀的所有集合；
* **永不自动删除**：主实例历史集合（191 个，含 125 个 `kb-lt-*`）。


---

# 附：真实 worker 专项复验（独立 PG + Redis DB15）

## A. 前置隔离件（**不得**指向主实例）

| 项 | 值 |
|---|---|
| 隔离 Qdrant | `http://127.0.0.1:56339`（**不是** 56333） |
| 独立 PG | 由 `KB_WORKER_DB_NAME` 指定（如 `kb_codex_linux_20261015_1053`） |
| 独立 Redis | `redis://127.0.0.1:56379/15`（**DB15**；绝不用主 DB0） |
| 向量前缀 | 本 session 子域 `kb-<KB_TEST_RUN_UUID>-w<短随机>` |
| raw / vector | 本 run 目录下 `artifacts/test-runs/<uuid>/{raw,vector_data}` |

端点传递已改为 **fail-closed**（`config/worker_test_settings.py` 在 star-import
之后直接绑定并校验）：
* 缺 `KB_WORKER_QDRANT_URL` → 拒绝启动；
* `KB_WORKER_QDRANT_URL` 含 `56333`（主实例）→ 拒绝启动；
* 缺 `KB_WORKER_RAW_STORE_DIR` / `KB_WORKER_VECTOR_DATA_DIR` → 拒绝启动。

实测：
```
缺 KB_WORKER_QDRANT_URL      -> RuntimeError: ...拒绝启动
指向主实例 56333             -> RuntimeError: 拒绝启动：...指向主实例
```

## B. 运行命令

```powershell
cd D:\AProjectScript\KnowledgeBase
$env:KB_PYTEST_DB = 'postgres'                      # 真 PG
$env:KB_PG_TEST_DB_NAME = 'kb_codex_linux_20261015_1053'
$env:KB_TEST_QDRANT_URL = 'http://127.0.0.1:56339'  # 隔离端点
$env:KB_QDRANT_URL      = 'http://127.0.0.1:56339'  # 显式，避免被 .env 覆盖

# 4 个 worker 专项
.\.venv\Scripts\python.exe -m pytest tests\test_real_worker.py -p no:randomly -q -v
# 期望：4 项真实执行（不再 skip），全部通过
```

## C. 通过判据（缺一不可）

1. **子 worker 写在隔离端点**：运行后 `56339` 上只应剩 `codex-runtime-probe-20261015`；
   `56333` 集合数必须仍是 **191**（未被写入）。
2. **worker 先停后清**：teardown 顺序必须是「停 worker → wait → 再清集合」，
   由 `_start_worker` 的 `finally` 与 `_parent_namespace` 的 finalizer 逆序保证。
3. **命名空间归本 run**：集合名形如 `kb-<session_uuid>-w<随机>-<lib>-g<n>`，
   会话清理的「本 run 前缀清点」能回收；清理不彻底会**显式失败**
   （已移除 `except Exception: pass`）。
4. **不碰主 Redis DB0**：broker 只用 DB15；`/0` 的队列消息数全程不变。

## D. 三方终检

```powershell
"主实例 = " + (Invoke-RestMethod http://127.0.0.1:56333/collections -TimeoutSec 5).result.collections.Count + " / 191"
"隔离端点 = " + (Invoke-RestMethod http://127.0.0.1:56339/collections -TimeoutSec 5).result.collections.Count + " / 1"
"media/raw = " + (Get-ChildItem media\raw -Recurse -File).Count + " / 271"
```

---

## 补充（实测回填，2026-10-15）

### 常规测试**无需手工清理**

`pytest` 会话结束会自动回收本 run 的集合、`raw/`、`vector_data/`，
并保留小型审计（`manifest.json` / `cleanup-report.json` / `heartbeat` / `run.lock`）。
清理失败会让 **session 失败**，不会静默。

### 入口一览（可直接执行）

| 入口 | 命令 | 实测结果 |
|---|---|---|
| 清理线专项 | `pytest tests/test_run_cleanup*.py -p no:randomly -q` | **88 项 / 0 失败 / 1 跳过** |
| 探活安全性 | `pytest tests/test_run_cleanup_probe.py -q` | **6 项全过**（含"探活不杀进程"） |
| 故意失败仍清理 | `.\.venv\Scripts\python.exe scripts/cleanup_failure_drill.py` | 子进程退码**恰为 1**；marker 集合已删、`failed=[]`、raw/vector 已清 |
| 压测全生命周期 | `.\.venv\Scripts\python.exe scripts/loadtest_runner.py --execute --files 5 --seconds 10 --run-id <12位hex>` | `202×5`、退码 **0**；停服务→清集合/raw/vector→**DROP 库** |
| 独立复验（对方） | 同上 | run `9af1b0c57d4e` / DB `kb_loadtest_bc142228`；独立查 DB 不存在、无本 run 集合、主 191/271 不变、DB0 队列 193 |

### 过程安全约束（真实踩过并已修）

* **禁止 `os.kill(pid, 0)`**：Python 3.13 在 Windows 上除 CTRL_C/BREAK 外
  **任何信号都调 `TerminateProcess`**（[官方文档](https://docs.python.org/3.13/library/os.html#os.kill)）
  —— 传 0 **也会终止进程**。已改为 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`
  + `GetExitCodeProcess` 的**只读**查询；未知/拒绝访问**保守视为存活**；
  并用**创建时间令牌**防 PID 复用。
* **常规测试结束无需手工清理**，也**不再用重试掩盖**未释放的锁：
  所有 `RunResources` owner 由 autouse 夹具在 teardown **无条件 release**
  （断言失败时同样执行）。去重试后 43 项仍全绿，证明锁确实已释放。

### 三方状态（复跑后必须一致）

```
主实例集合 = 191     media/raw 文件 = 271     Redis DB0 队列 = 193
隔离端点（56339） = 仅 codex-runtime-probe-20261015
历史 vector_data/qdrant = 36441 文件（**不在**自动删除范围内）
```
