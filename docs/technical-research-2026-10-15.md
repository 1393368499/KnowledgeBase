# 实际故障与官方资料核对

## Qdrant 存储

实际证据：`kb-9-g1` 的 query_points 返回500，含 `OutputTooSmall` panic；查询独立验收库11可正常返回两份资料。`docker inspect knowledgebase-qdrant` 确认当前挂载为 Windows `D:/AProjectScript/KnowledgeBase/vector_data/qdrant` → Linux容器 `/qdrant/storage`。

官方文档明确记录不兼容文件系统及Windows共享目录的同类问题；维护者在问题记录中建议使用WSL内部文件系统。这与当前故障和部署方式吻合，属于有依据的根因判断；仍需用迁移与重启完整性试验确认，不能以一次重建成功代替长期修复。

- https://github.com/qdrant/landing_page/blob/master/qdrant-landing/content/documentation/common-errors.md
- https://github.com/qdrant/qdrant/issues/6758
- https://github.com/qdrant/qdrant/issues/7646

需满足的两项要求：实际存储为Linux ext4等支持的文件系统；向量数据物理载体仍位于项目根目录。建议评估专属WSL发行版，将其ext4.vhdx安放在项目 `vector_data/` 中。微软官方支持从可信容器镜像导出rootfs并指定位置导入WSL，避免全局迁移Docker Desktop磁盘或克隆用户现有发行版。

- https://learn.microsoft.com/windows/wsl/use-custom-distro
- https://learn.microsoft.com/windows/wsl/disk-space

部署验收必须包括实际findmnt/filesystem证据、原数据冷备与哈希清单、健康集合数据/查询前后核对、损坏验收库新generation重建、原件指纹不变、服务与WSL重启后向量持续可读，以及回退演练。旧目录及旧集合不得直接删除。先保持已测试的Qdrant版本，分离存储迁移和版本升级变量。

## 模型预算与协议

实测128 token预算可得到HTTP200但空content；正常预算能够生成正文。vLLM官方说明推理输出与最终正文是不同字段，工具调用也不应从推理草稿解析。当前修复必须拒绝空输出和length截断，不能把推理内容或半份文档作为成功结果。

- https://docs.vllm.ai/en/stable/features/reasoning_outputs/

本轮不得修改AS711服务启动参数；应用侧预算、协议适配和真实请求验证应先完成。未来如采用请求级推理开关或结构化输出，需先核对实际服务器支持并进行隔离对照，不能以文档版本替代实机验证。

## 后台任务

Celery当前官方FAQ说明不支持Windows。Windows solo可作为开发验证手段，不能证明生产并发。真正运行和并发验收应使用Linux容器或WSL worker，并验证队列绑定、重试、租约、看门狗心跳与重启恢复。

- https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows

现有业务Redis含历史验收消息，不能清空或无差别消费；新运行环境需要专用队列及消息内容哈希/库绑定。部署脚本必须防止dotenv覆盖容器内数据库/Redis地址，且不得输出密钥。

## 开发模型请求中断（09:28）

实际开发会话持续显示请求重试且没有新代码。直接打开官方 https://status.deepseek.com/ 显示“DeepSeek Web/API Partially Unavailable”，V4.1 Flash/Pro及网页服务受影响，正在实施修复。旧搜索摘要或另一statuspage的正常状态不能作为当前可用性证据。官方错误码文档建议500/503短暂等待后重试；重试应有界，不能把无限等待称作开发进度。

- https://status.deepseek.com/
- https://api-docs.deepseek.com/quick_start/error_codes/

可用替代是在现有DeepseekHarness中调用已配置且经实测的AS711模型，继续遵守实现方写代码、Codex审查的分工；不修改远端服务配置。

## 迁移恢复版本与空间条件补充

Qdrant当前官方恢复文档约束快照可在同minor或下一minor恢复；迁移指南提醒还原窗口同时存在快照和解压目标、需额外磁盘。本轮先保持实测1.15.5，避免把文件系统迁移与跨多个minor升级混在一起。冷态压缩归档方案应标明它是应用停止后的完整目录归档，不冒充Qdrant API snapshot；零区稀疏化及物理空间仍须实测。

- https://qdrant.tech/documentation/operations/snapshots/
- https://qdrant.tech/documentation/migration-recovery-options/

## Linux任务中断恢复

Celery官方任务文档区分Python异常重试与worker崩溃重投递：幂等任务可评估acks_late；子进程异常终止即使晚确认仍可能确认，reject_on_worker_lost需要另行评估。Redis visibility_timeout过短会使长任务重复投递，过长会拖延丢失任务恢复。因此不能只启动prefork就宣称可靠并发，必须联合现有CAS租约/有限重试预算做实际中断与重复投递验收。

- https://docs.celeryq.dev/en/stable/userguide/tasks.html
- https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html

## 迁移执行器收敛（11:03）

PowerShell执行草稿反复出现退出绕过catch、原生参数解析、缺少pgrep、逐文件启动数万次WSL的问题。已要求实现方使用项目现有Python3.13环境管理冷备/manifest/异常回退，并由静态Linux脚本批量执行复制校验，PowerShell仅保留薄包装。Python官方接口支持ZipFile.open流式访问条目、hashlib流式摘要及shutil.disk_usage读取实际宿主余量；不需新增第三方依赖。该方案仍必须经实际夹具、故障与正式迁移验收。

- https://docs.python.org/3.13/library/zipfile.html
- https://docs.python.org/3.13/library/hashlib.html#hashlib.file_digest
- https://docs.python.org/3.13/library/shutil.html#shutil.disk_usage
