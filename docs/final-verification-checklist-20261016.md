# 最终复验清单（2026-10-16，给独立复验方）

按顺序执行；每条都写了"预期"和"什么算失败"。**不要**在业务库/主队列上跑，不要把
`kb-audit-4e8ae96e` / `kb-final-ui-4e8ae96e` 的资源当测试资源清理。

## 0. 构建与发布（统一镜像 + 显式迁移）
```bash
powershell -File deploy/build-app.ps1            # 产物 kb-app:delivery（含 LibreOffice/CJK/psutil/python-pptx）
docker compose -f deploy/docker-compose.unified.yml --env-file .env \
  up -d --force-recreate migrate web worker scheduler
```
预期：`web/worker/scheduler` 三个容器的 `.Image` **同一个 sha256**；`migrate` 一次性退出码 0；
镜像内自检：
```bash
docker run --rm --entrypoint sh kb-app:delivery -c 'soffice --version | head -1'
docker run --rm --entrypoint sh kb-app:delivery -c 'python -m pip show psutil python-pptx | grep -iE "^(Name|Version)"'
docker run --rm --entrypoint sh kb-app:delivery -c 'fc-list :lang=zh | wc -l'
```
预期：`LibreOffice 25.2.3.2` 起、`psutil 7.2.2` + `python-pptx 1.0.2`、中文字体数 ≥ 1（本机为 30）。
迁移状态：`docker exec knowledgebase-web python manage.py showmigrations knowledge planner` 到
`knowledge.0017_vectorcollection_retiring_at`、`planner.0005_*` 全部 `[X]`。

## 1. 登录链接（必须真实访问并登录）
```
http://127.0.0.1:8800/app/login/     （账号仍是既有 admin，未被改动）
```
预期：登录后落在 `/app/files/`；`curl -s http://127.0.0.1:8800/api/v1/health/ready/` 返回
`{"status":"ok","checks":{"database":"ok","migrations":"ok","vector_data":"ok"}}`。

## 2. 九项用例矩阵（SQLite，隔离）
```bash
KB_PYTEST_DB=sqlite python -m pytest tests/test_reset_library_continuation.py \
  tests/test_reset_library_index.py tests/test_rerank_degradation.py tests/test_unarchive_file.py \
  tests/test_document_export.py tests/test_export_document_real.py tests/test_legacy_office_parsing.py \
  tests/test_global_breaker_state.py tests/test_model_probe_targets.py tests/test_llm_probe_target.py \
  tests/test_search_dedupe.py tests/test_worker_settings_guard.py -o addopts=""
```
预期：**252 passed / 23 skipped / 0 failed**（跳过项须写明原因；工作树加了两条新用例后为 253）。
整仓：`python -m pytest tests -o addopts=""` → **986 passed / 51 skipped / 0 failed**。

## 3. 真实 PG（含并发互斥）
```bash
KB_TEST_DB=postgres … python -m pytest tests/test_cleanup_activation_lock_pg.py \
  tests/test_reset_library_continuation.py --kb-db=postgres -o addopts=""
```
预期：**18 passed**。其中必须包含：
- 清理持库行锁期间，另一连接的 `SELECT … FOR UPDATE` 拿不到锁；
- **真实** `activate_generation` 在锁被持有时阻塞、放行后读到退役状态并拒绝；
- **真实** `rollback_generation` 拒绝退役代次；
- 远端 drop **第一次失败 → 登记与退役状态都保留 → 第二次同入口重试成功才回收**。

## 4. 真实 worker（需要**独立** Qdrant 与独占 broker）
```bash
KB_TEST_BROKER_URL=redis://redis:6379/7 KB_TEST_QDRANT_URL=http://qdrant:6333 \
  python -m pytest tests/test_real_worker.py --kb-db=postgres --kb-broker --kb-qdrant -o addopts=""
```
预期：5 例通过。注意：若 `KB_WORKER_QDRANT_URL` 指到**主实例**，`worker_test_settings`
会**拒绝启动**（这是有意的安全闸门，不是缺陷）；子进程连接凭据由 `connection.settings_dict`
显式下传，缺 `KB_PG_PASSWORD` 键即 fail-closed。

## 5. 界面回归（发布端口）
```bash
python artifacts/acceptance/verify-removed-and-restored-entries.py 8800
```
预期：**16/16**（四个模型管理控件与「重置数据库」彻底消失；`测试连通`×4、上传/扫描目录/全部解析、
`重置索引`×3、`删除信息库`×3 保留；无 JS 控制台错误）。
归档行：`解析` 按钮应为 **disabled**，tooltip "已归档：普通解析不会恢复它，请通过「恢复」重新纳入检索…"；
勾选归档行做批量解析时应**跳过并提示**。

## 6. 导出与可视核对
```bash
# 容器内真实导出（HTTP 端点）
python artifacts/acceptance/final-ui-seed.py     # 独占环境；仅在通过四道守卫时执行
```
预期：3 份合成资料 HTTP 201/DONE/indexed；`final-ui-export.docx` / `.pdf` 生成；脚本在任何上传非 DONE
或导出失败时**退出码非 0**。
渲染核对（本机已生成）：`artifacts/acceptance/render/final-ui-docx-page1.png`
——标题只出现一次、表格占满正文宽度、"12 个月"不折行。

## 7. 搜索去重（独立复现的重复）
预期：同一份资料、同一段正文在结果里**只出现一条**；同一份资料的**不同段落**必须都保留；
不同资料即使正文相同也都要保留；只差词界的 `"AB C"` 与 `"A BC"` **不得**被合并。

## 8. 熔断跨进程
预期：web 侧产生的熔断状态 worker 侧**立刻可见**（同一 `opened_at`）；受控 `reset_breaker`
之后两侧都看不到，且 Redis 里无残留；Redis 不可用时如实 `scope=process/redis_available=false`
且**不**声称全局成功。

## 9. 环境红线（每条都必须保持）
- Redis **DB0 不消费**：`redis-cli -n 0 llen celery` 应保持 **193**。
- 业务库保持 `3 库 / 0 文件 / 0 分段 / 0 反馈`。
- 原件与原型不可变：`artifacts/acceptance/user-fixes-baseline-20261016.json` 的
  `raw_sha256`（277 项）与 `protected_sha256`（2 项）逐一比对应全部一致。
- 不启停 AS711 任何服务；Qdrant 命名卷保留；admin 账号不改。

## 10. 已知未闭合（不是"通过"，如实列出）
1. 真实 worker 5 例**必须在有独立 Qdrant 的环境**跑；本机（单一 Qdrant）会被安全闸门拒绝启动。
2. **（本轮已闭环）** LibreOffice 对隔离 profile 安全键的**采纳**已用真实转换验证：读 LO 退出时
   写回的 `user/registrymodifications.xcu`，7 个键全部以**非默认值**出现、伪键未被采纳；
   证据 `artifacts/acceptance/lo-adoption-20261016.json`（`not_adopted=[]`、`bogus_adopted=[]`），
   工具 `artifacts/lo_verify_adoption.py`（已修掉它自身两个会误报"未采纳"的缺陷）。
3. Qdrant 主实例存在历史未登记测试集合 `kb-bad9bbe1360a-wd6963d-3-g1`，业务库有若干
   `library_id IS NULL` 的历史登记：均为既有残留，**未**由开发方删除，等授权。
