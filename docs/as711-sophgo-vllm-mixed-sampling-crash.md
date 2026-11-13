# AS711 算能 vLLM-TPU 引擎崩溃报障材料

> 用途：提交给算能（Sophgo）技术支持。可整篇转发，不含业务敏感信息。

## 1. 环境

| 项 | 值 |
|---|---|
| 主机 | AS711（172.16.100.114，hostname `yu08`） |
| 加速卡 | Sophon TPU（8 chip，/opt/tpuv7，CHIP_MAP=2,3） |
| 镜像 | `sophgo/vllm-tpu:v0.19.0-rc.1-20260911`（镜像构建时间 2026-09-11，容器创建 2026-09-12） |
| 容器 | `vllm-qwen3.x-35b-fp8`，端口 10001→8000，restart=no |
| 模型 | `/data/models/Qwen3.6-35B-A3B-FP8`（`served-model-name=Qwen3.6-35B-A3B-FP8`） |
| 引擎版本 | vLLM V1（engine 报 `v0.19.0`），`compilation_config.backend='eager'`，`quantization=tpu_fp8` |
| 关键启动参数 | `--tensor-parallel-size 2 --max-num-seqs 8 --max-num-batched-tokens 262144 --override-generation-config '{"temperature": 0.0}' --additional-config '{"fp8_activation_scheme":"dynamic"}' --cudagraph-capture-sizes 1 2 3 4 5 6 7 8 --reasoning-parser qwen3 --tool-call-parser qwen3_coder --enable-auto-tool-choice` |

## 2. 现象

引擎在正常服务 4 天后，**单次请求即触发 EngineCore 致命错误**：

```
(Worker_TP0 pid=215) ERROR [multiproc_executor.py:949]   File "/venv/lib/python3.12/site-packages/vllm_tpu/sample/tpu_sampler.py", line 387, in sample
(Worker_TP0 pid=215) ERROR [multiproc_executor.py:949]     sampled = torch.where(
(Worker_TP0 pid=215) ERROR [multiproc_executor.py:949] NotImplementedError: Could not run 'aten::where.self_out' with arguments from the 'tpu' backend. ... 'aten::where.self_out' is only available for these backends: [CPU, Meta, ... , AutogradPrivateUse1, ...]
(EngineCore pid=145) ERROR [core.py:1110] RuntimeError: Worker failed with error 'Could not run 'aten::where.self_out' with arguments from the 'tpu' backend. ...'
(APIServer pid=1) ERROR [async_llm.py:707] vllm.v1.engine.exceptions.EngineDeadError: EngineCore encountered an issue.
(APIServer pid=1) INFO: Shutting down
```

容器随后 `Exited (0)`（非 OOM，`OOMKilled=false`），端口 10001 消失，所有客户端得到 `Connection refused`。

已发生两次（+08）：**2026-10-19 08:55:27**、**2026-10-19 09:21:07**。

## 3. 触发条件（可复现）

同一 batch 内**同时**存在 greedy 请求（`temperature` < `_SAMPLING_EPS`，含服务端默认 0.0）和 random 请求（`temperature` > `_SAMPLING_EPS`）时必现。

崩溃前的 scheduler dump 证实批内为两条不同采样模式的请求：

```
scheduled_new_reqs=[NewRequestData(req_id=chatcmpl-8dc611d81abce3bf-a8c13924,
    prompt_token_ids_len=4564, sampling_params=SamplingParams(
    temperature=0.2, top_p=0.95, top_k=20, max_tokens=4096, ...))]
scheduled_cached_reqs=CachedRequestData(req_ids=['chatcmpl-af4c458c0a1f758b-ad679cd6'],
    num_computed_tokens=[71421], num_output_tokens=[9380])   # temperature=0.0（greedy）
num_scheduled_tokens={...: 4564, ...: 1}, total_num_scheduled_tokens=4565
```

第二次崩溃的批内组合：一条 `temperature=0.0, max_tokens=32000, prompt_token_ids_len=62042` 的请求 + 一条 `temperature=0.2` 的请求。

复现步骤（在 AS711 上，服务空闲时）：

```bash
# 1) 先发一条长 greedy 请求，占住 engine step（后台）
curl -s http://127.0.0.1:10001/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.6-35B-A3B-FP8","messages":[{"role":"user","content":"详细分析光速变成每秒一米会发生什么"}],"max_tokens":2000,"temperature":0}' &

# 2) 在它仍在 decode 时，立刻发一条 temperature>0 的请求
curl -s http://127.0.0.1:10001/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.6-35B-A3B-FP8","messages":[{"role":"user","content":"你好"}],"max_tokens":32,"temperature":0.2}'
```

预期（当前版本）：第 2 条请求返回 500，日志出现 `NotImplementedError: aten::where.self_out`，容器退出。
`--max-num-seqs` 设为 1 时不复现（批内只有一种采样模式），说明与 batch 内混采直接相关。

## 4. 代码定位

镜像内 `/venv/lib/python3.12/site-packages/vllm_tpu/sample/tpu_sampler.py` `Sampler.sample()`（约 387 行）：

```python
        if greedy_sampled is None:          # all_random → 提前返回，不触发
            return random_sampled, processed_logprobs

        sampled = torch.where(
            sampling_metadata.temperature < _SAMPLING_EPS,
            greedy_sampled,
            random_sampled,
            out=greedy_sampled,             # ← aten::where.self_out
        )
        return sampled, processed_logprobs
```

`all_greedy`、`all_random` 两种纯批次都会提前 return，**只有混采批次会走到 `torch.where(..., out=...)`**；`torch_tpu` 后端未注册 `aten::where.self_out`（out 变体），于是整批 worker 抛 `NotImplementedError`，EngineCore 判定 fatal 并退出，属于"一个请求拖垮整个引擎"。

## 5. 建议修复（三选一，按侵入性排序）

1. **不传 `out=`**：`sampled = torch.where(mask, greedy_sampled, random_sampled)`。若 `aten::where.self` 已在 TPU 后端注册，改动最小。
2. **改用索引填充**（不依赖 `where`）：
   ```python
   sampled = random_sampled
   m = sampling_metadata.temperature < _SAMPLING_EPS
   if m.any():
       idx = m.nonzero(as_tuple=True)[0]
       sampled[idx] = greedy_sampled[idx]
   ```
3. **在 torch_tpu 侧补齐 `aten::where.self_out` 的算子注册**（对所有用户更通用）。

另建议：单请求采样失败不应升级为 EngineCore fatal（可降级为该请求报错 + 批次重试），否则任何算子缺口都会变成整引擎宕机。

## 6. 我们侧的临时规避（不修改镜像）

- 让所有客户端统一使用 greedy（本机队默认即为 `temperature` 缺省 0.0），使混采批次不出现；
- 若无法统一所有客户端，可临时 `--max-num-seqs 1` 串行化（吞吐代价大，仅作兜底）；
- 配合 `/v1/models` 探活告警，避免容器退出后长时间静默不可用。

## 7. 期望

- 确认上述混采崩溃是否已知问题；
- 提供含修复的镜像 tag（我方仅需更新部署 `.env` 的 `IMAGE_TAG`，不改镜像内容）；
- 如需我方提供更多日志/复现，可随时配合（崩溃日志与 scheduler dump 已保留在容器日志中）。

## 8. 我方应用侧已落地的对策（2026-10-19，服务器一行没动）

目标只有一个：让**本系统**永远不当"混采"里的 random 那一半。

| 位置 | 改动 | 作用 |
| --- | --- | --- |
| `knowledgebase/settings.py` | 新增 `KB_LLM_GREEDY_ONLY`（默认 1）、`KB_LLM_TEMPERATURE`（默认 0.2，仅在开关关闭时生效） | 显式的止血开关；同时补上"`KB_LLM_TEMPERATURE` 原先只出现在 `getattr` 兜底里、写进环境变量并不生效"的缺陷 |
| `apps/planner/llm.py` | 新增 `effective_temperature()`；`default_profile()` 与 `chat()` 的请求体都经过它 | **单点出口**：即便有人手搓 `LlmProfile(temperature=0.3)` 或用 `replace` 造随机 profile，发出去的 `temperature` 仍是 0.0 |
| `apps/knowledge/classification.py` | 只补注释（本来就是 0.0） | 保持与对话同口径，防止后人改回随机 |
| `tests/test_llm_sampling_mode.py` | 新增回归：默认 greedy、手搓 profile 被压、开关关闭后恢复、分类同口径 | 防止被"顺手调回 0.2" |
| `.env.example` | 补 `KB_LLM_GREEDY_ONLY=1` / `KB_LLM_TEMPERATURE=0.2` | 部署时看得见这个口径 |

代价与边界：

- **代价**：回答不再随机（同一问题重复问结果一致）。这是拿"多样性"换"可用性"。
- **边界**：只能保证**本系统**不发作 random 请求。机队里别的客户端（如 `.40` / `.51`）
  若哪天开始发 `temperature>0`，混采仍会出现——那不属于本仓库能控制的范围，
  届时仍需第 5 节的镜像修复或第 6 节的 `--max-num-seqs 1`。
- **恢复**：厂家镜像修好后设 `KB_LLM_GREEDY_ONLY=0` 即回到 `KB_LLM_TEMPERATURE=0.2`，不改代码。
- **验证**：`pytest tests/test_llm_sampling_mode.py`；运行期日志出现一次
  `采样统一为 greedy：本应发出的 temperature=… 被压成 0.0` 即为生效。

## 9. 交付镜像已重建并滚动生效（2026-10-19 09:50，用户授权"重建镜像、删旧镜像"）

服务器仍未动一行；只重建了**本系统自己的**应用镜像。

| 步骤 | 命令/结果 |
| --- | --- |
| 构建 | `powershell -File deploy/build-app.ps1`（交付路径 A，基座 `kb-worker:phase2-review-20261015` 本地 image ID 校验通过 `sha256:63c6532e…`） |
| 产物 | `kb-app:delivery` = `sha256:53f09fbfc824…`（2026-10-19 09:50 构建） |
| 滚动更新 | `docker compose up -d`（根入口）→ web / worker / scheduler 全部重建，`migrate` 复跑（`No migrations to apply`，不可篡改触发器就位） |
| 旧镜像 | 原 `kb-app:delivery` = `f6364b6bd5db`（08:47 构建）重建后**已不在本机**（containerd 镜像存储回收未打 tag 的旧层；`docker images -a` 无 dangling、无重复 tag），无需再手工 `docker image rm`；`kb-worker:*` 基座镜像保留（下次重建仍需要） |
| 验证 | 三容器 `healthy`；容器内 `greedy_only=True`、`effective_temperature(0.2)=0.0`、`default_profile().temperature=0.0`；容器内对 AS711 **真实调用**两次（默认档；手搓 `temperature=0.9` 的 profile）均正常返回；`/api/v1/health/ready/` = 200；AS711 `/v1/models` = 200（未被本轮调用打挂） |
| 遗留边界 | 本系统从此不发 random；若机队其他客户端发 `temperature>0`，混采仍会复现——那属于厂家镜像/服务端问题（第 5、6 节），与本次部署无关。 |
