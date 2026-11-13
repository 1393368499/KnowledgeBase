# Linux worker / beat 设计与部署

> 独立审查条目 11：本文件此前**不存在**却被矩阵引用。现已补齐；
> 文中每条都对应仓库里的真实文件与实测结果，**不把计划当实现**。

## 1. 为什么必须 Linux

Windows 上 Celery **官方不支持**生产级并发：prefork 池不可用，只能 `solo`（单进程串行）。
此前所谓"并发验收"用的是 ASGI 多 worker，**不是** Celery 并发，不能当生产证据。

## 2. 交付物

| 文件 | 作用 |
|---|---|
| `deploy/worker/Dockerfile` | worker/beat 镜像；**显式 COPY**（不用 `COPY . .`）；无 apt 依赖 |
| `deploy/docker-compose.worker.yml` | worker + beat 两个服务；专用队列；只读挂载原件 |
| `deploy/worker/worker.env.example` | 地址与口令模板（真实 `worker.env` 已在 `.gitignore`） |
| `knowledgebase/celery.py` | 专用队列路由 + `knowledge.sweep_pending` 周期任务 |
| `knowledgebase/settings.py` | `KB_CELERY_BROKER_URL`/`KB_CELERY_RESULT_BACKEND` 映射 + beat 调度 |
| `.dockerignore` | 密钥/数据/虚拟环境排除（第二道防线） |

## 3. 隔离设计（四条硬要求）

1. **专用队列** `kb_worker_v2`；worker 的 `-Q` 只认它。
2. **专用 Redis DB**：`KB_CELERY_BROKER_URL` 指向非 0 的 DB（示例 `/15`），
   业务 DB0 里的历史消息**永不被消费或清空**（绝不 `purge`）。
3. **路由键必须匹配真实任务名**：本应用任务是 `knowledge.*`
   （`knowledge.process_ingested_file`、`knowledge.sweep_pending`）。
   早期写成 `apps.*` → **一个都匹配不到**，任务会落回默认队列。
   回归测试用 Celery **真实 router** 判定：
   `tests/test_celery_queue_routing.py`（4 项，全过）。
4. **`KB_SKIP_DOTENV=1`**：项目 `.env` 用 `override=True` 加载，
   容器里若不跳过，会把地址覆盖回 `127.0.0.1`，容器内连不上 PG/Redis/Qdrant。

## 4. 原件挂载（否则 worker 读不到数据）

原件真实路径是 **`data/media/raw`**（`RAW_STORE_DIR`，由 `knowledgebase.paths.raw_store_dir()` 决定）。
worker 解析需要读它，因此 compose 里以**只读**方式挂载：

```yaml
volumes:
  - ../data/media/raw:/app/data/media/raw:ro
```

只读是刻意的：源资料不可修改（硬约束）。容器内 `RAW_STORE_DIR` 必须指向该路径。

## 5. beat 的真实调度

beat 不是空壳：调度里挂的是**已有且有用**的 `manage.py kbprocess`
（扫待处理/失败资料的轻量看门狗），周期默认 300s，可用
`KB_CELERY_BEAT_SWEEP_SECONDS` 调整、设 0 关闭。

schedule 文件放在**宿主绑定目录** `deploy/worker/beat-state`：
Docker Desktop 下 uid 10001 对该目录可写；命名卷曾导致不可写、beat 起不来。

## 6. 构建注意（实测）

- `python:3.13-slim` **已自带** ca-certificates，worker **不需要 curl**；
  实测在 Debian 镜像上 `apt-get install` 会因上游 500/503 `exit 100`，
  因此镜像里**不装任何 apt 包**；健康检查用 `celery inspect ping`
  （不依赖 curl，也**不依赖 Qdrant** —— 迁移停写窗口 Qdrant 会被有意停掉，
  那时被判 unhealthy 会造成误导）。
- `requirements.txt` 里 `pywin32==312; sys_platform == "win32"`：
  不加平台标记，Linux 上没有该发行包会**直接构建失败**。
- 显式 COPY 后构建上下文实测约 **1.6 MB**。

## 7. 尚未闭合（独立审查 12，不得当作已完成）

- **崩溃恢复未接线**：`process_ingested_file` 捕获 `PipelineError` 后返回
  `ok=false`，**不会触发 Celery retry**；`max_retries=3` 只是声明。
  也未见 `acks_late` / `reject_on_worker_lost`。
  需在"有限重试 + 租约幂等预算"下设计，并用**隔离任务**验证：
  杀 worker 子进程 → 恢复 → 重复投递 → 最终状态一致。
- **跨进程熔断状态**（独立审查 5）：`CircuitBreaker` 是进程内 `BREAKERS`，
  web 与 worker 不共享。监控/重置**不能**只重置当前进程就宣称全系统状态，
  必须标注进程范围或实现跨进程共享。
