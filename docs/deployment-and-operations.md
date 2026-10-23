# 部署与运维（P7）

> **2026-10-15更新**：完整目标仍在开发与严格验收。AS711真实问答与文档生成已另行验证，存储迁移和Linux后台任务仍在独立审查中；当前迁移草稿不能直接用于真实数据。本文旧的“唯一待补项”结论已失效，详情见 [恢复记录](resume-2026-10-15.md)。AS711其他服务不得启停或修改；本地模型范围已由用户明确为AS711目标模型。

> 本文只写**可执行的事实**：命令、路径、口径。任何未实测的容量数字都不写。
>
> **状态说明（2026-10-14）**：P1–P7 已实现，并由审查方完成**独立功能验收与
> 隔离并发验收**（权威证据：`docs/acceptance-2026-10-14.md`；
> 汇总：`docs/delivery-status.md`）。本文描述的是**当前生效**的行为与命令。
>
> **唯一待补项：AS711 真实模型侧验收**（真实资料问答 + AI 生成文档）。
> 另注意：该服务**物理重启后不会自启**，需人工拉起（见 §3.1）；
> 模型不可用时助手会**如实 503**，不会伪造回答。

---

## 1. 组件与端口（本机 docker compose）

| 组件 | 容器名 | 宿主端口 | 数据位置 |
|---|---|---|---|
| web（gunicorn） | `knowledgebase-web` | **`8800`** → 容器内 8000 | bind mount `media/raw`（原件） |
| migrate（一次性） | `knowledgebase-migrate` | 不映射 | —（跑完即退出，失败则应用服务不启动） |
| worker / scheduler | `knowledgebase-worker` / `-scheduler` | 不映射 | docker volume |
| PostgreSQL 17.6 | `knowledgebase-postgres` | `55432` | docker volume |
| Redis 7.4 | `knowledgebase-redis` | `56379` | docker volume |
| Qdrant v1.15.5 | `knowledgebase-qdrant` | `56333`/`56334` | **docker 命名卷** `knowledgebase-qdrantdata-delivery-20261015` |

> **web 的宿主端口是 8800，不是 8000**：Windows 的 WinNAT/Hyper-V 会动态预留成段的 TCP 端口，
> 落在预留段里的端口宿主绑定会被拒（`WinError 10013`）。实测 7984–8083 吃掉过 8000，
> 且换 `0.0.0.0`/`127.0.0.2`/`::1` 都无效。容器内始终是 8000，只有宿主映射可换；
> 需要临时换端口时设 `KB_WEB_HOST_PORT` 再 `up -d --force-recreate --no-deps web`。
> **绑定地址**也可换：`KB_WEB_BIND`（默认 `127.0.0.1` 只给本机；要给内网访问才设 `0.0.0.0`）。

### 1.1 首次部署（全新机器，按顺序执行）

```bash
bash deploy/preflight.sh      # 检查 .env、建 external 卷、把绑定目录交给 uid 10001
bash deploy/build-app.sh      # 两步建镜像：kb-worker:local-build → kb-app:delivery
docker compose up -d          # **从仓库根**：根 .env 才会参与 ${} 插值（根 compose 已 include 本文件）
docker compose exec web python manage.py kbcreateuser --username X --password Y --staff
curl -sS http://127.0.0.1:8800/api/v1/health/ready/     # 期望 200 且 checks.migrations == "ok"
```

Windows 上把前两步换成 `powershell -File deploy/build-app.ps1`（preflight 的等价动作见下表末行）。

**这些前提都不是"理论风险"**，全是部署审查在本仓库实测出来的阻断：

| 前提 | 不做的后果（实测原文） |
|---|---|
| external 卷先存在 | `up` 直接中止：`external volume "knowledgebase-pgdata" not found` |
| 先建镜像，或设 `KB_BASE_IMAGE` | `deploy/app/Dockerfile` 的 `FROM` 是基座 tag，新机器没有它 → BuildKit 去 Docker Hub 解析 `kb-worker` 而失败 |
| `media/raw` / `media/templates` / `deploy/worker/beat-state` 属主 10001 | 原件或模板上传写不进去；beat 建不了 `celerybeat-schedule`（预检脚本已统一准备；Windows 上由 Docker Desktop 兜住，Linux 上必须 `chown`） |
| 根 `.env` 里 `KB_PG_PASSWORD` 等 | 用 `-f deploy/docker-compose.unified.yml` 时根的 `.env` **不参与** `${}` 插值 → postgres 用默认口令初始化、应用读真口令 → `password authentication failed for user "kb"`（要么从根跑，要么加 `--env-file .env`） |
| 管理员账号 | 镜像**不会**自动建账号；迁移与触发器现在由 `migrate` 服务自动跑（它成功退出后 web/worker/scheduler 才启动） |

> 容器里 `DB_ENGINE` 与 `DJANGO_DEBUG` 由 compose **钉死**（`KB_DB_ENGINE`/`KB_DJANGO_DEBUG`，
> 默认 `postgres` / `0`）：`.env.example` 里 `DB_ENGINE=sqlite`、`DJANGO_DEBUG=1` 是**宿主开发**口径，
> 照抄进容器会让三进程退化到 SQLite 并让 debug 页对外可见。

```powershell
docker compose up -d
docker compose ps            # web/worker/scheduler/postgres/redis/qdrant 都应 healthy；migrate 是 Exit 0
```

向量数据放在 **docker 命名卷**里（用户 2026-10-15 明确授权：原 bind mount
`vector_data/qdrant` 在 Qdrant 重启后会出现 payload 读取失败，重建新代次仍 7/7 复现）。
容器内路径仍是 `/app/vector_data`，所以 `config/paths.py` 的"必须在项目根内"校验照常通过；
旧目录**原样保留**（不删除、不拷贝已坏数据），新卷从空开始、由 `kbrebuild` 按原件重建。

---

## 2. 应用启动

后台巡检按资料 ID 分批轮转，并将检查范围、每轮 ID 上界和下一批位置保存在
`PatrolRun.coverage`。周期与手动巡检各自续做；一轮开始后的新增资料在下一轮检查，
避免持续上传让旧资料永远轮不到。控制台的“本批检查”只代表该批结果。
更新到此版本时需应用 `knowledge.0023_patrolrun_coverage` 迁移，再更新应用容器。

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py guards          # 触发器自检
.\.venv\Scripts\python.exe manage.py kbcheckinstall  # 从零安装验证
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8800
```

生产口径用 ASGI：`.\.venv\Scripts\python.exe -m uvicorn config.asgi:application --host 0.0.0.0 --port 8800`。

> 端口用 8800 的原因见 §1 的说明；8000 在本机可能被 Windows 保留段吃掉而直接 bind 失败。

### ★ 触发器刷新迁移必须先应用（否则删除通道不生效）

改**触发器代码**（`governance/triggers.py`）**不会**自动更新数据库里已经存在的
触发器——DDL 是要显式执行的。与删除通道相关的两条刷新迁移：

| 迁移 | 作用 |
|---|---|
| `knowledge.0010_delete_guard_uses_grant` | 资料表 DELETE 改为查 `kb_delete_grant`（事务局部授权） |
| `knowledge.0011_chunk_delete_guard_uses_grant` | **分段表** DELETE 同样按 `OLD.file_id` 授权放行（否则级联删除必被挡） |

两者都依赖 `planner.0002_deletionjob_deletegrant`（授权表 `kb_delete_grant`）。
**部署时务必确认 `migrate` 无残留**：

```powershell
.\.venv\Scripts\python.exe manage.py showmigrations knowledge planner | Select-String "0010|0011|0002"
```

核对"线上函数真的换了"（`pg_get_triggerdef` 只看函数名，要看**函数体**）：

```sql
SELECT proname, position('kb_delete_grant' in pg_get_functiondef(oid)) > 0 AS grant_aware
FROM pg_proc WHERE proname LIKE 'kb_guard%';
-- kb_guard_ingested_file_delete_guard / kb_guard_chunk_delete_guard 都应为 true
-- kb_guard_append_only_guard 应为 false（审计仍无条件拒绝删除）
```

**禁止**：手写 `DROP TRIGGER` 或让应用在运行时"临时摘掉守卫"——那会制造
全局删除窗口。授权是**事务局部**的（授权行与删除在同一事务，未提交对他人不可见）。

---

## 3.1 模型服务（AS711，172.16.100.114）

四个模型服务以 **Docker 容器**方式运行在 AS711 上，分属三个 compose 项目，
且 **`RestartPolicy=no`**——**物理重启后不会自启**。
典型故障表现：主机 `ping` 通、`sshd` 在（能连上并回 `Permission denied`），
但 10001/8000/8002/8003 **无监听**，客户端看到"连接被拒绝"或超时。

| 项目目录（`/home/openclaw/workspace/deploy/`） | compose 文件 | 容器 → 宿主端口 |
|---|---|---|
| `qwen3.5` | `qwen3.x-35b-fp8-tp2.yaml` | `vllm-qwen3.x-35b-fp8` → **10001** |
| `bge` | `bge-large-zh-v1.5-tp1.yaml` + `bge-reranker-large-tp1.yaml` | embedding → **8000**、rerank → **8002** |
| `paddleocr` | `paddleocr-vl-1.5-tp1.yaml` | `vllm-paddle-ocr` → **8003** |

```bash
cd /home/openclaw/workspace/deploy/bge
docker compose -f bge-large-zh-v1.5-tp1.yaml -f bge-reranker-large-tp1.yaml up -d

cd /home/openclaw/workspace/deploy/paddleocr
docker compose -f paddleocr-vl-1.5-tp1.yaml up -d

cd /home/openclaw/workspace/deploy/qwen3.5
docker compose -f qwen3.x-35b-fp8-tp2.yaml up -d
```

就绪判据（大模型加载需数分钟，期间端口可能尚未监听）：

```bash
for p in 10001 8000 8002 8003; do ss -ltn | grep -q ":$p " && \
  curl -s -o /dev/null -w "$p -> %{http_code}\n" --max-time 4 http://127.0.0.1:$p/health; done
```

四项都 200 后，在本项目侧用 `manage.py kbprobe --probe` 复核
（含**合理尺寸**的 OCR 文本样本——用 1×1 图片会被 PaddleOCR 的处理器拒绝，
造成"模型离线"的假阴性）。

**队列模式**（推荐生产）：

```powershell
# .env: KB_UPLOAD_PROCESS_MODE=queue, CELERY_TASK_ALWAYS_EAGER=0
.\.venv\Scripts\python.exe -m celery -A config worker --concurrency 2 --loglevel INFO
```

没有 worker 时用 `manage.py kbprocess` 在进程内推进（只处理租约空闲的资料）。

---

## 3. 关键运维命令

| 用途 | 命令 |
|---|---|
| 触发器自检/补建 | `manage.py guards [--ensure] [--json]` |
| 模型服务只读探测（**默认不发请求**） | `manage.py kbprobe [--probe] [--profile bge1024]` |
| 建验收账号 | `manage.py kbcreateuser --username X --password Y [--staff] [--library code --role admin]` |
| 推进待处理资料 | `manage.py kbprocess [--file N] [--retry-failed]` |
| 索引初建/换模型重建 | `manage.py kbrebuild --library code [--all]` |
| 从零安装验证 | `manage.py kbcheckinstall` |

---

## 4. 运行期开关（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_UPLOAD_PROCESS_MODE` | `auto` | `inline` / `queue` / `auto`（派发不出去就如实"待处理"） |
| `CELERY_TASK_ALWAYS_EAGER` | `0` | **生产必须 0**；设为 1 会让"队列"退化成请求内同步执行 |
| `KB_MODEL_CALLS_DISABLED` | `0` | **离线模式**：模型外呼立刻失败且不发请求（AS711 未就绪时用） |
| `KB_SEARCH_DENSE_ENABLED` | `1` | 设 0 时检索只走关键字 |
| `KB_TEST_ADAPTER` | 空 | 仅 `offline` 时启用确定性假向量（只写 `__offline_test` collection） |
| `KB_PROCESS_LEASE_SECONDS` | `900` | 处理租约；长任务续租，过期可被接管 |
| `KB_EMBED_TIMEOUT_SECONDS` | `10` | **检索用**嵌入超时（短请求） |
| `KB_EMBED_INDEX_TIMEOUT_SECONDS` | `60` | **入库用**嵌入超时（后台任务，可放宽） |
| `KB_OCR_MAX_PAGES` | `20` | 单份 PDF 最多 OCR 页数；超过**报错中止**（不静默丢页） |
| `KB_ACTION_TICKET_TTL_SECONDS` | `900` | 一次性确认凭证有效期 |
| `KB_CELERY_TASK_TIME_LIMIT` | `1800` | 任务**硬超时**（秒）：到点父进程杀掉任务，消息按 `task_acks_on_failure_or_timeout` 确认，不会变成毒消息循环 |
| `KB_CELERY_TASK_SOFT_TIME_LIMIT` | 硬超时的 4/5（`1440`） | 软超时：给任务一次优雅失败的机会（写状态/留审计） |
| `KB_CELERY_VISIBILITY_TIMEOUT` | 硬超时 + 300（`2100`） | **崩溃恢复 SLA**：worker 被硬杀后，在飞消息最长等这么久才重新投递。必须**大于**任务硬超时（见下） |
| `KB_MODEL_LIMIT_ENABLED` | `1` | **跨进程模型并发闸门**开关（设 0 只用于对照实验） |
| `KB_MODEL_MAX_CONCURRENCY` | `4` | 每个模型服务允许的**同时在飞**外呼数，额度在 **Redis 里跨进程共享**（web 与 worker 共用一份） |
| `KB_MODEL_SLOT_WAIT_SECONDS` | `10` | 拿不到额度时最多等多久；等不到按"服务不可用"如实上报，**不放行** |
| `KB_MODEL_SLOT_TTL_SECONDS` | `300` | 额度成员的存活时间；持有者被 SIGKILL 时靠它自愈。**必须大于单次外呼最坏耗时**（最坏是 `KB_LLM_DOC_TIMEOUT_SECONDS=180`） |
| `KB_MODEL_LIMIT_PREFIX` | `kb:model:slots` | 闸门的 Redis 键前缀（与业务队列的键完全不重叠） |
| `KB_MODEL_LIMIT_REDIS_URL` | 空（复用 `CELERY_BROKER_URL`，即专用 **DB14**） | 闸门用的 Redis；**不要**指向 DB0（那是共享业务队列） |

### 安全基线开关（2026-10-22 审计收口，`config/settings.py`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DJANGO_SECRET_KEY` | 开发占位值 | `DJANGO_DEBUG=0` 时**必须显式设置**；仍是那个公开占位字面量时，导入期直接 `ImproperlyConfigured`（会阻止容器启动）。pytest 进程改用进程内随机密钥，不要求测试环境配密钥。 |
| `KB_HTTPS_ONLY` | `0` | 置 `1` 一次性打开 HTTPS 全套：`SECURE_SSL_REDIRECT`、`SECURE_PROXY_SSL_HEADER`（`X-Forwarded-Proto: https`）、`SESSION_COOKIE_SECURE`、`CSRF_COOKIE_SECURE`、HSTS（含子域与 preload）。默认关闭是因为本机部署形状是 `http://127.0.0.1:8800` 明文回环。 |
| `KB_HSTS_SECONDS` | 打开时 `31536000`，否则 `0` | HSTS 时长；建议先设小值（如 `3600`）试跑，确认没有明文混用再调大。 |
| `KB_CSRF_TRUSTED_ORIGINS` | 空 | 逗号分隔的可信来源，例如 `https://kb.example.com`；经反代换域名访问时**必填**，否则表单 POST 会 403。 |

> 探针脱敏：`/api/v1/health/live/` 与 `/api/v1/health/ready/` 无需鉴权（容器 `HEALTHCHECK` 没有凭据），因此响应体**只含状态词**（`ok` / `error` / `pending` / `missing`）；数据库异常原文、未应用迁移名、向量库绝对路径只写服务端日志。

### 跨进程模型并发闸门（2026-10-15 实装，`apps/knowledge/ratelimit.py`）

**为什么进程内熔断器不够**：`resilience.CircuitBreaker` 是**进程内**状态，多进程下"同时打给 AS711 的请求数"也是每进程各算一份；web（检索/对话）与 worker（入库嵌入）同时开跑时会叠加，而 AS711 上四个模型服务跑在同一台机器上。

实现口径（与"熔断"不是一回事：熔断是失败后短路，闸门是限制并发）：

| 事项 | 口径 |
|---|---|
| 额度存储 | Redis **排序集合** `kb:model:slots:<service>`（member=本次占用的令牌，score=获得时刻） |
| 取额度 | 原子 Lua：先 `ZREMRANGEBYSCORE` 清掉超 TTL 的**单个**持有者，再在 `ZCARD < limit` 时 `ZADD` |
| 放额度 | `ZREM` 自己的令牌 |
| 为什么不用 `INCR/DECR` | 持有者被 SIGKILL 会**永久占死**额度；而给键加整体 TTL 又会在负载持续时把**在飞**的持有者也清零 → 超发 |
| 挂点 | **唯一外呼收口** `resilience.call_with_resilience()` → embedding/rerank/ocr/llm 四条路径一起生效；闸门满了抛 `ServiceUnavailable` 且**不发请求**，也不计入熔断失败 |
| Redis 不可用 | **fail-open 放行**（不能因 Redis 抖动让检索/入库停摆），降级次数计入指标 `model_concurrency.fallback` |
| 指标口径 | `model_concurrency.scope = "redis"`（与进程内熔断器 `breakers.scope = "process"` **分开标注**） |

验收证据：`artifacts/acceptance/cross-process-limit.json`（4 个**独立操作系统进程** × 25 轮，Redis 采样到的全局在飞峰值 = 限额 2、有真实拒绝、结束额度归还）、`tests/test_cross_process_rate_limit.py`（14 项，含"父进程占满 → **子进程**必须拿不到"这条核心断言）。复现：`.\.venv\Scripts\python.exe artifacts/acceptance/verify_cross_process_limit.py`。

**边界（如实登记）**：这解决的是**并发数**，不是**速率/令牌桶**（没有"每秒 N 次"的限制）；宿主上跑任何 Celery 相关命令前请确认 broker 是**专用 DB14**（`.env` 已改；DB0 是共享业务队列，绝不能投任务进去）。

### 崩溃恢复的真实边界（2026-10-15 实测，`scripts/crash_recovery_probe.ps1`）

`task_acks_late` / `task_reject_on_worker_lost` 只覆盖**一半**场景，两者的实测数字完全不同：

| 场景 | 谁 reject | 实测恢复时间 | 证据 |
|---|---|---|---|
| 杀 worker 的**池子进程**（父进程还活着） | 父进程（`task_reject_on_worker_lost`） | **13.8s**（200 条全部 SUCCESS，未确认清零） | `artifacts/acceptance/worker-crash-recovery.json` → `case_a` |
| **硬杀整个容器**（宿主宕机 / OOM / `docker kill`） | 没人 | 只能等 kombu 的 `visibility_timeout` | 同上 → `case_b` |

硬杀为什么只能等：kombu `transport/redis.py` 把消息记进 `unacked_index` 的 score 是**"变为未确认的时刻"**（L369），重新投递的判据是 `age > visibility_timeout`（L415/L419），默认 `visibility_timeout = 3600`。**修复前实测**：硬杀后 4 条在飞消息还要 **3554s（≈59 分钟）**才回来，期间重启的 worker 把队列里其余 196 条做完、这 4 条一直 `PENDING`；修复后把 `visibility_timeout` 收敛为 `硬超时 + 300`，同样场景在 `visibility_timeout` 内自动恢复。

为什么 `visibility_timeout` **必须大于**任务硬超时：小于就会出现"上一个任务还活着、消息已被重新投递" → **同一份资料被处理两遍**。这就是为什么本次同时加了两个任务超时（此前**两个都没设** = 任务可以永远不返回，任何 `visibility_timeout` 都不安全）。回归测试 `tests/test_celery_crash_recovery_settings.py` 锁住了这两个关系。

复现命令（隔离队列 `kb_crash_probe`，主队列 `kb_worker_v2` 与 Redis DB0 全程不动）：

```powershell
pwsh -File scripts/crash_recovery_probe.ps1 -Count 200 -VisibilityTimeout 30
```

`KB_MODEL_CALLS_DISABLED` 与 `KB_TEST_ADAPTER` **不是一回事**：前者什么都不写，
后者会写假向量（因此只允许写测试专用 collection）。

---

## 5. 故障处置

| 现象 | 处置 |
|---|---|
| 检索结果远少于预期 | 看 `/api/v1/knowledge/search/` 的 `report.degraded`：语义不可用时会标注，此时关键字那一路仍工作 |
| 资料处理失败 | 文件管理页点"重新处理"；失败不会丢原件（`raw_path` 保留） |
| 删除后仍能搜到残留内容 | 检索返回前会按当前数据库事实过滤已删除资料（`report.tombstoned_dropped` / `tombstoned_reasons` 记数量与原因），**fail-closed**：指纹或库 id 缺失即丢弃。该过滤已由隔离回归覆盖（见 `docs/delivery-status.md` §8）。清理任务未完成时在助手首页"清理与重试"里重试 |
| 向量库宕机 | 检索降级为数据库关键词；恢复后用 `manage.py kbrebuild` 校验索引 |
| 模型服务连不上（**AS711 重启后常见**） | AS711 上四个模型服务是 Docker 容器且 `RestartPolicy=no`，**物理重启后不会自启**；用 `ss -ltn` 确认端口无监听，再按 §3.1 的原 compose 方式拉起。期间本系统表现为"如实 503 + 模型不可用提示"，不要误判为系统故障 |
| 断路器打开导致调用快速失败 | `manage.py kbprobe` 查看各服务熔断状态；确认服务恢复后再重置 |
| 待处理资料积压 | 起 worker，或 `manage.py kbprocess --limit N` |
| `docker compose up -d web` 报 `ports are not available: exposing port TCP 127.0.0.1:<端口> ... bind: An attempt was made to access a socket in a way forbidden by its access permissions` | 宿主端口落进了 Windows 的动态保留段（WinError 10013），**不是本系统的错**。诊断：`netsh int ipv4 show excludedportrange protocol=tcp`，看目标端口是否被某段覆盖。三种处置：① 换宿主端口：`$env:KB_WEB_HOST_PORT='8800'; docker compose -f deploy/docker-compose.unified.yml --env-file .env up -d --force-recreate --no-deps web`（项目默认已是 8800，且该段外）；② 拿回原端口（**需管理员**）：`net stop winnat` → `docker start knowledgebase-web` → `net start winnat`（先让容器占住端口，WinNAT 重新规划时会避开在用端口）；③ 重启 Docker Desktop，让 HNS 重新分配保留段 |
| 重建镜像后页面还是旧的 | 模板与静态资源**烘进镜像**：先 `powershell -File deploy/build-app.ps1` 重建 `kb-app:delivery`，再 `docker compose -f deploy/docker-compose.unified.yml --env-file .env up -d --force-recreate --no-deps web`；用 `docker exec knowledgebase-web grep -c '<新标记>' <容器内文件>` 证明容器不是旧镜像 |

| `docker compose up -d` 报 `external volume "knowledgebase-pgdata" not found` | 全新机器上 external 卷还不存在。先 `bash deploy/preflight.sh`（或 `docker volume create knowledgebase-pgdata knowledgebase-redisdata`）。**不要**把 `external: true` 去掉——那会新建空卷、等于丢数据 |
| 构建时报 `pull access denied for kb-worker` / `manifest unknown` | `deploy/app/Dockerfile` 的基座 tag 在这台机器上不存在。跑 `bash deploy/build-app.sh`（产出 `kb-worker:local-build`）并设 `KB_BASE_IMAGE=kb-worker:local-build`；Windows 用 `powershell -File deploy/build-app.ps1`（本机有历史基座时会自动走校验路径） |
| 用 IP/域名访问得到 **HTTP 400 DisallowedHost**，而容器内 `curl localhost` 仍 200 | `DJANGO_ALLOWED_HOSTS` 没有该主机名。compose 现在按 `KB_ALLOWED_HOSTS` 插值（默认 `127.0.0.1,localhost,web,testserver`）→ 在根 `.env` 里补上服务器 IP/域名再 `up -d` |
| `knowledgebase-web` 起不来、`migrate` 是 Exit 非 0 | 应用服务 `depends_on: migrate: service_completed_successfully`：先看 `docker compose logs migrate`。迁移失败时**不会**启动应用（这是有意的 fail-fast，避免半套结构对外服务） |
| `ready` 探针 503 且 `checks.migrations` 为 `pending: N ...` | 库结构落后于代码：`docker compose exec web python manage.py migrate`（探针故意不再只 `SELECT 1`——空库也报 healthy 曾是假绿灯） |
| scheduler 显示 `unhealthy` | 旧版判据只看 `celerybeat-schedule` **主文件** mtime，而 beat 用 SQLite WAL（运行期几乎只写 `-wal`/`-shm`）→ 启动约 10 分钟后**必然**转 unhealthy（实测 Up 31 minutes）。**现判据已改为"调度文件族里最新的那个 mtime 在 10 分钟内"**；仍在报的话说明容器跑的是旧镜像：重建镜像 + `up -d --force-recreate` |
| 只重建了 web，worker/scheduler 还是旧代码 | `docker compose up -d` **不会**因为镜像 tag 相同而重建未改配置的容器。改完代码要 `docker compose up -d --force-recreate`（一次覆盖三个应用服务） |

**禁止操作**：不要 `docker compose down -v` 清 Qdrant 以外的卷而不备份；
不要手动 `DROP TRIGGER`（删除通道已改为"事务局部授权"，摘触发器会制造全局删除窗口）。

---

## 6. 统一测试（系统开发完成后执行）

```powershell
# 离线全量（不需容器/不需模型）
.\.venv\Scripts\python.exe -m pytest tests --kb-db=sqlite

# 真实 PG + 真实 Qdrant + 真实 worker
# **并行/多人同时跑时，必须用 KB_PG_TEST_DB_NAME 指定真正的测试库名**
# （Django 会在它前面再加 test_）。只设 KB_PG_TEST_DB 不够：
# 那个变量决定的是"配置里的 NAME"，而实际跑的是 TEST.NAME →
# 不加区分就会多个会话抢同一个库（实测撞过 test_kb 被占用）。
$env:KB_PYTEST_DB='postgres'
$env:KB_PG_TEST_DB_NAME='kb_verify_<你的标识>'   # ★ 真正隔离的关键
.\.venv\Scripts\python.exe -m pytest tests --kb-qdrant --kb-broker

# AS711 真实模型用例：**默认不执行**（标记为 real_as711 的用例会被 skip）
#
# ★ 必须显式打开开关，否则即使写了 -m real_as711 也仍然全部 skip：
$env:KB_TEST_AS711='1'          # 不设或为空 → 跳过全部 real_as711 用例
.\.venv\Scripts\python.exe manage.py kbprobe --probe
.\.venv\Scripts\python.exe -m pytest -m real_as711
```

> `-m real_as711` 只负责**选择**用例；是否真的执行由 `KB_TEST_AS711` 决定
> （见 `tests/conftest.py` 里的 skip 逻辑）。**默认不执行**，
> 这是为了避免在模型不可用时把"跳过"误读成"通过"。

**已独立验收（最终）**：

- 独立 PG + 真实 Qdrant + 隔离真 worker：**296 passed / 1 skipped**，108.70s
  → `artifacts/acceptance/final-postgres.xml`（skip = AS711 实机用例）
- SQLite 离线：**284 passed / 13 skipped**，23.43s
  → `artifacts/acceptance/final-sqlite.xml`（skip 为真实基础设施与 PG 专项，**不计为通过**）
- 隔离并发：新库首批上传 **20/20**、同库第二批 **20/20**、8 并发关键词检索 **116/116**；
  8 并发确认 **1×200+7×400**、8 并发执行 **1×200+7×409**
  （后两者的拒绝是**预期互斥**，不是可用率）。

详见 `docs/acceptance-2026-10-14.md` 与 `docs/delivery-status.md`。

**仍待补**：AS711 真实资料问答 + AI 生成文档（模型侧）；
当前**不声明整个模型链路验收完成**。

真实 worker 用例的隔离口径：独立 PG 测试库（`KB_PG_TEST_DB_NAME`）+ Redis DB15
+ 随机队列 `kb-test-<uuid>` + 随机 collection 命名空间 `kb-t-<uuid>`，
结束只删自己的队列/结果键/collection，**绝不 purge 业务队列**。
