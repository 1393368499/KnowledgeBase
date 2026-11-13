# 模型接入运行手册（AS711 四路模型）

> 场景：系统其余功能已全部可用、模型还没连上（或换了端点）。本手册是**把四路模型
> （嵌入 / 重排 / OCR / LLM）接上并验证到"全功能可用"的唯一顺序**，每一步都给真实
> 判定命令与期望值，不用"应该可以"结案。
>
> 2026-10-15 记录：当天上午 AS711 曾在线，四路模型**真实调用成功**，产物
> `artifacts/acceptance/as711-resume-20261015.json`（`manage.py kbprobe --probe`，
> embedding `actual_dimension=1024` 与声明一致、rerank `top_score=0.97265625`、
> OCR ready）与 `artifacts/acceptance/resume-independent-rag.json`（真实 LLM 问答
> "36个月/赵启明"+ 来源引用，60.57s，usage 真实）。下午起 AS711 从宿主与容器都不
> 可达，模型相关能力进入降级态 —— 这正是本手册要恢复的状态。

## 一、先证明网络（两层都要，容器路径可能与宿主不同）

```powershell
# 宿主侧（四个端口：嵌入 8000 / 重排 8002 / OCR 8003 / LLM 10001）
Test-NetConnection 172.16.100.114 -Port 8000
```

```powershell
# 容器侧（关键：宿主通不代表容器通，Docker 网络是独立路径）
docker exec knowledgebase-web python -c "import socket;socket.create_connection(('172.16.100.114',8000),3);print('ok')"
```

一键体检（宿主 + 容器可达性 + 真实探测 + 健康检查）：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\model-readiness.ps1
```

## 二、配置：默认值已可用，改端点/模型/口令才需要动 `.env`

默认值在 `knowledgebase/settings.py:457-478`，登记清单在 `.env.example:118-131`。**仓库根
`.env` 当前不含这些变量**（即当前走代码默认值），要改就在 `.env` 里加同名变量。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_MODEL_API_KEY` | 空 | 口令只走环境变量，绝不写进仓库 |
| `AS711_LLM_BASE_URL` / `AS711_LLM_MODEL` | `http://172.16.100.114:10001/v1` / `Qwen3.6-35B-A3B-FP8` | 问答与文档生成 |
| `AS711_EMBED_BASE_URL` / `_MODEL` / `AS711_EMBED_DIM` | `...:8000/v1` / `bge-large-zh-v1.5` / `1024` | 维度不符会抛 `DimensionMismatch`，**不补零** |
| `AS711_RERANK_BASE_URL` / `_MODEL` | `...:8002/v1` / `bge-reranker-large` | 检索精排 |
| `AS711_OCR_BASE_URL` / `_MODEL` / `_MAX_NUM_SEQS` | `...:8003/v1` / `PaddleOCR-VL-1.5` / `16` | 扫描件与图片 |
| `KB_MODEL_CALLS_DISABLED` | `0` | 置 `1` 时所有模型外呼立刻以"服务未启用"失败（`.env.example:91`） |

超时与限额（`.env.example:93-103`、`knowledgebase/settings.py:50-53`、`:423-440`）：
LLM 60s / LLM 文档 180s / 嵌入 10s（入库 60s）/ 重排 15s / OCR 120s / Qdrant 10s；
`KB_SEARCH_USE_RERANK=1`、`KB_RERANK_TOP_N=20`、`KB_SEARCH_DENSE_ENABLED=1`。

## 三、让配置生效（改 env 不需要重建镜像，但必须重建容器）

```powershell
docker compose -f deploy/docker-compose.unified.yml --env-file .env up -d --force-recreate migrate web worker scheduler
```

- **必须显式带上 `migrate`**：一次性迁移服务（`deploy/docker-compose.unified.yml:213`）
  只单独指定 `web` / 或加 `--no-deps` 时**不会重跑**，会出现"镜像更新了但迁移没应用"。
- 只有改了 JS/模板/CSS 才需要 `deploy/build-app.ps1` 重建镜像（静态资源烤进镜像）。
  **四个容器必须同一镜像**：`docker inspect --format '{{.Config.Image}} {{.Image}}' knowledgebase-web knowledgebase-worker knowledgebase-scheduler`。

## 四、三级真实验证（逐级都过才算"模型接上了"）

1. **真实调用探测（权威）**
   ```powershell
   docker exec knowledgebase-web python manage.py kbprobe --probe --json
   ```
   期望：`embedding.ready=true` 且 `actual_dimension=1024`；`rerank.ready=true` 且
   `top_score>0`；`ocr.ready=true`。形态样板见 `artifacts/acceptance/as711-resume-20261015.json`。
   （`kbprobe` 覆盖嵌入/重排/OCR；LLM 用第 3 步的真实问答验证。）

2. **界面「系统配置 → 测试连通」**（`GET /api/v1/knowledge/models/?probe=1`）
   **口径差异必须知道**：嵌入探测会真算一个 1024 维向量；重排/OCR **只**发一次
   `GET {base}/models` 做连通性（`apps/knowledge/views.py:749`，`connectivity_only=true`）；
   LLM 是最小 `chat/completions`（`:790`）。**若 AS711 的 `:8002`/`:8003` 没有实现
   `/v1/models`，界面可能显示"未连通"而服务其实可用** —— 以第 1 步的 `kbprobe --probe`
   为准（这也是本手册把第 1 步定为权威的原因）。

3. **端到端冒烟（用真实资料）**：登录 `admin` → 文件管理上传一个 `.txt` → 等解析/入库
   完成（状态"已完成"）→ 搜索页检索（应出现向量召回与重排后的次序）→ 助手页提问
   （答案应带来源引用）。上传一个扫描件/图片可顺带验证 OCR。

## 五、故障排查

- **熔断**：同一进程连续 3 次失败后打开，冷却 60s（`apps/knowledge/resilience.py:77-78`）。
  熔断状态是**进程内**的：重启容器即复位。界面会如实显示"熔断打开"。
- **降级**：熔断打开或 `KB_SEARCH_DENSE_ENABLED=0` 时检索退化为关键字并在结果里带
  `degraded` 说明；重排失败退回 RRF 融合并把原因记进 `rerank.error`。
- **队列错配（静默、最值得检查的一条）**：compose 给生产者写死
  `KB_CELERY_QUEUE=kb_worker_v2`（`deploy/docker-compose.unified.yml:62`），worker 用
  `-Q ${KB_CELERY_QUEUE:-kb_worker_v2}`；若在宿主 `.env` 把它覆盖成别的值，任务会被投到
  没有消费者的队列 → **任务永不执行且不报错**。检查：
  ```powershell
  docker exec knowledgebase-worker celery -A knowledgebase inspect active_queues
  ```
- **健康检查**：`Invoke-WebRequest http://127.0.0.1:8800/api/v1/health/ready/`（`checks.migrations` 应为 `ok`；
  路由前缀来自 `knowledgebase/urls.py:13` → `knowledgebase/health_urls.py` 的 `live/` 与 `ready/`）。

## 六、边界（如实登记，不在本手册假装已解决）

- 页面上的"启动 / 切换本地模型"按钮是原型遗留、**故意禁用**并带 title 说明（模型相关）。
- 代次切换（激活/回滚向量代次）走页面受控流程（二次确认 + 审计），没有 CLI。
- 若宿主带 HTTP 代理：模型外呼走 `build_http_client`（`trust_env=False`，不读代理变量）；
  但界面重排/OCR 连通性探测用的是裸 `httpx.get`（`views.py:749`），可能被代理影响 ——
  这也是以 `kbprobe --probe` 为权威的另一个理由。
