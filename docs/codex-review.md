# Codex 审查记录

## 最新原型界面验收结论（2026-10-14）

本次界面还原已通过独立浏览器验收。原型文件哈希未变，原型CSS除注释空白外一致；首页仅新增可收起文档栏，使用说明独立成页。PG+真实Qdrant+隔离worker 328通过/1跳过，SQLite316通过/13跳过，最后小修后的定向回归24通过。详细证据与原型占位能力、AS711待联调限制见 [ui-acceptance-2026-10-14.md](ui-acceptance-2026-10-14.md)。下文保留历史审查过程，已关闭问题以最新验收记录为准。

## P1 第一轮独立验证（2026-10-13）

执行 `.venv/Scripts/python.exe -m pytest --tb=short`：71 passed，1 failed，1 warning（20.15s）。这是中途快照，P1尚未正式交付。

需实现方修复：
1. `test_file_stats_action` 在创建第三份样本时，conftest 的 make_file 生成重复 content_sha256，触发 uniq_file_per_library_hash。请用确定性且唯一的样本指纹，不取消真实去重约束。
2. `VectorCollectionViewSet` 当前 `queryset=VectorCollection.objects.all()` 未按 readable_library_ids 过滤，普通用户可能看到无权限库的集合和存储路径。请过滤库权限，避免泄露内部路径，并修复无排序分页警告；加入越权测试。
3. 先前UI反馈中的 Django版本、不可篡改绕过、审计链并发锁问题仍待正式验收。
4. P2 上传须真正持久化原始字节并校验哈希；目前upload只存元数据，不能把此阶段功能宣称为完整文件入库。

## 新需求

用户：向量模型改成2048维度，后续会更换模型。
已同步实现方：目标默认2048维，当前真实bge的1024维作为明确兼容profile，不补零或截断；模型版本/端点/维度配置化；新模型新collection、全量重嵌入后切换、旧索引可回退；响应维度错误必须拒绝。

## P1 第二轮独立验证

升级 Django 6.1.2 后独立运行全量测试：72 passed（25.74s），首次fixture失败与分页警告已消失。此结果尚不能证明新增数据库守卫/2048配置已经完整通过，仍待补专项测试。

P4之前必须修正的设计：`governance/triggers.py` 注释计划在受控删除时drop_guards/create_guards，且drop_guards移除所有UPDATE、DELETE和审计表触发器。这会给并发请求打开修改窗口。生产请求路径严禁通过DROP TRIGGER或禁用全局触发器来删除。请使用事务/连接范围的精确目标删除授权（必须绑定已通过三次确认的operation、对象id、actor、会话，且只允许DELETE；UPDATE和审计守卫始终有效）。迁移/测试管理工具可以维护触发器，不能暴露为AI工具。

## P1 基础工程验收通过（功能范围限定）

独立确认：Django 6.1.2；manage.py check零问题；makemigrations --check --dry-run无差异；默认实际连接vendor=postgresql，guards_present=True；同一环境pytest全量94 passed（30.08s）；向量profile实测1024 ready=True、2048未配置ready=False；三个compose服务healthy；Qdrant挂载源为 D:/AProjectScript/KnowledgeBase/vector_data/qdrant。

P1通过范围：基础工程、只读/追加数据底座、基础权限API、profile维度校验。不包含真实上传落盘、解析/检索、删除确认、AI操作或写文档，也未验证超大并发。

P2验收关注：
- 依赖manifest必须完整固定版本，支持干净环境重装。
- BatchEmbedder目前丢弃生成vectors，P2必须真正upsert并以Qdrant检索证据验收。
- 重建必须使用独立generation collection，不得复用正在查询的active collection；失败不能破坏旧索引；DB唯一约束保证每库最多一个active，并发切换须锁库行。
- 全库检索不能只查默认库；库下拉筛选要贯穿API权限过滤、向量过滤及前端。
- 文件落盘/解析/SQL/向量写入失败需可重试，不能把半成品标记done；原始字节始终只读，重试不能改已完成分段。
- P4仍须实现真实DB事务级精确目标删除授权；当前contextvar声明本身不够，严禁运行时drop_guards。

## P2 离线开发审查（用户确认 AS711 断开，2026-10-13 20:15起）

无需恢复AS711即可完成开发。真实模型联调保持待恢复；测试适配器不得写正式索引或宣称为真实模型结果。

代码中可直接确认的待修项（由DeepseekHarness写代码和回归测试）：
1. pipeline.embed_chunks已构造qdrant，但调用upsert_points(client, ...)仍传原始可为None的参数。生产默认路径必然失败，必须传qdrant；加不注入client的回归测试。
2. parsers._parse_pdf对blank_pages[:ocr_pages]只处理前20页，随后可宣称整份成功，超限扫描页静默遗漏。请分页批处理或明确失败/待处理，不能截断成功。
3. parsers._parse_xlsx空工作簿blocks=[]时访问blocks[0]会IndexError；应显式ParseError，补空工作簿测试。
4. pipeline.write_chunks逐条save无事务，中途错误会留下不可补齐的半份分段；重试只按数量判等，未核对text/page/ordinal。请原子写入并按内容校验幂等。防止同文件并发process重复解析/嵌入，任务需锁/lease。
5. vectorindex.prepare_generation使用active_generation+1。并发构建在未激活时重用同generation，回退后也会重用已有generation。请独立单调counter/max登记generation或UUID，禁止复用active/旧/失败generation。激活需验证plan属于库、已完整重建且模型维度匹配；不能把空索引直接切上线。
6. pipeline.search在embed_texts连接失败时直接退出，无法继续关键词检索。离线时应明确展示语义/重排不可用，仍能使用本地关键词检索；不能将关键词结果称为语义结果。
7. 上传接口同步执行整条解析/模型流水线，远端掉线时长时间阻塞请求，与并发要求冲突。改为持久化后队列处理、202和任务状态；失败不丢原件，可在服务恢复后重试。没有worker时应明确待处理，不能假装完成。
8. httpclient.clean_proxy_environment修改进程全局os.environ，多个线程会互相恢复错误快照。我已核对本机qdrant_client/http/api_client.py，ApiClient的kwargs直接传httpx.Client；请验证直接QdrantClient(...,trust_env=False)，移除运行时全局环境修改。注释中“with内return返回__exit__结果”的Python语义错误也请修正。

本机Docker Engine此刻不可连接；我已用Hidden方式启动已安装Docker Desktop，若仍不可用请明确记录，测试可另用独立SQLite+Qdrant embedded临时目录，不能影响正式数据。

## P2 续接独立审查（2026-10-13）

已通过侧边栏恢复 DeepseekHarness 会话，并即时发送上一轮排队的8项审查。当前三个项目容器均 healthy。AS711 依用户要求继续不探测。

中途测试快照：`pytest --kb-db=sqlite --tb=short` 得到150 passed、13 failed、2 skipped；13项失败均由测试 helper 与 make_file 重复传入 vector_collection 导致。此命令的数据库选择尚未可靠：tests/conftest 顶层导入模型早于 pytest_configure 写入 KB_TEST_DB，不能据开关声称 SQLite 已验证。显式设置 KB_TEST_DB=sqlite 后独立 django.setup 确认为 sqlite。

新增待修与验收要求：
- services.ingest_file 新建记录时没有把传入 blob 的 raw_path/raw_size_bytes 写入，也没有调用 _record_blob；首次上传记录缺少原件路径，process API 会直接拒绝。须通过真实 multipart API 覆盖首次上传、重复上传和失败重试，不能只测手工构造的模型。
- search 按 active_generation 拼接 collection；新库尚未嵌入的已解析分段无法搜到。需对已授权的 DocumentChunk 实现本地关键词降级，AS711 或 Qdrant 不可用时仍有真实关键词命中，不能将 embedded 的断言改为0来绕过。
- 本机 qdrant_client/local/payload_filters.py 明确支持 MatchText，采用 whole-token matching；中文连续长串会导致短查询不匹配。"embedded不支持payload索引"不等于"无法关键词匹配"，需要确认分词行为与实际召回。
- process_file 默认 ocr=None，没有创建 OcrClient；扫描PDF/图片的生产默认入口无法使用OCR。ServiceUnavailable 和 Qdrant 异常也未统一转FAILED，可能长留RUNNING。须补默认路径、失败状态与重试测试。
- rerank/ocr 尚未接入韧性层；CircuitBreaker.guard 半开时全量放行，并无单探测门闩，应避免恢复瞬间的并发请求全部打到故障服务。
- 后续PG集成测试使用与DeepseekHarness不同命名的独立测试库，避免并发争用test_kb；数据库与collection测试结果必须明示真实后端、适配器开关和跳过原因。

### 续接复测与索引设计要求

- 已独立通过：pip check、manage.py check、makemigrations --check --dry-run；kbcheckinstall 在新建的临时 SQLite 上从零迁移并确认 guards=True。主环境仍为 PostgreSQL。
- SQLite 与独立命名 PostgreSQL 测试库中途快照均在第83项失败：test_full_offline_pipeline_writes_vectors_and_searches 的 dense_hits=0。此时阶段尚未交付，不据此声称全量通过。
- 新增字段曾导致0002运行时触发器引用 OLD.parse_lease_token，让SQLite从零迁移失败。历史迁移应冻结当时列集合，新schema通过后续迁移刷新保护；逆迁移同样必须可运行。
- 每次 prepare 分配从未使用过的新generation。旧测试要求重复prepare复用代次，已明确应更新过时断言，不能为使旧测试通过重新引入空collection复用。如果以后需要幂等，只能绑定显式build_id。
- 同一资料可同时存在g1/g2，单个 IngestedFile.vector_collection_name/embedded_chunk_count 不能证明每一代完整。激活/回退必须分批 retrieve 目标collection的稳定point_id，核对真实points的file、ordinal、text/hash、模型版本与维度。写新代期间不修改当前生效指针，校验放在锁库事务内，成功后原子切换。测试需要在FakeQdrant或embedded中真正写points，不能只修改file元数据宣称构建完成。
- 新建OcrClient.available=False，解析器若据此预拒绝会永远无法首调用。需区分configured（允许尝试）与proven（成功证据），真实调用受超时/重试/熔断限制。

P2尚未验收。仍需队列化上传/同文件任务互斥、真实multipart上传到处理的回归覆盖、文件与搜索Web UI和交付记录；AS711真实联调待恢复验证。

### 续接底层复测通过（功能范围限定）

独立全量：显式KB_PYTEST_DB=sqlite，170 passed/2 skipped；显式postgres、与Harness不同命名的测试库、--kb-qdrant，171 passed/1 skipped（AS711）。这些是补上传API/队列/UI之前的快照，不代表P2整体完成。已确认检索失败根因是search自行拼collection名，绕开了真实登记的测试后缀；实现方已改为按库实际指针/collection登记取目标与profile。

队列审查阻塞项（20:53）：生产settings与.env.example的CELERY_TASK_ALWAYS_EAGER仍默认True，Celery app import时快照设置，auto/queue的apply_async实为请求内同步运行；派发成功时HTTP分支也可能返回201而不是202。必须默认非eager、明确202/task_id、有限broker发布超时。同文件process_file无跨worker lease，kbprocess直接领取RUNNING会争抢活任务；要求带token与到期时间的DB原子claim、仅过期可恢复、完成/释放按owner token条件更新。单靠write_chunks事务与点id稳定不能防止重复解析/重复消耗模型或旧worker改状态。

### 本轮后端交付后的独立复测（21:15起）

独立命名PostgreSQL测试库 + 真实Qdrant：182 passed/1 skipped，唯一跳过AS711；再次kbcheckinstall全新SQLite迁移guards=True。P2整体仍未通过，已要求DeepseekHarness继续UI、非eager真实worker与文档，未向用户请求重复确认。

当前源码仍需用worker/重建回归测试解决的具体问题：
- process_file的DONE与次数上限检查仍在hold_lease之前；acquire_lease会refresh，但随后直接_process_file_locked，旧实例可能在另一worker已完成后再次处理。应在claim后用最新状态决定跳过。
- 过期RUNNING的接管会再次调用_guarded_status(RUNNING,bump_attempts=True)，模型明确禁止同状态递增；且此调用在try之前，可能仍停RUNNING。应测试过期RUNNING恢复，原子claim同时分配尝试号/状态，不能仅测试pending。
- 新增lease与rebuild_library没有对应专门回归用例（当前rg tests中的lease/rebuild只匹配旧注释）。现有182项通过不足以证明新并发保护正确。
- rebuild_library对每份资料force=True仍受全生命周期MAX_PARSE_ATTEMPTS限制，正常多次换模型/重建会耗尽3次解析上限；已完成的只读分段应复用，重嵌入不得消耗文件失败重试预算。
- 重建目标分支目前仍写IngestedFile当前vector_collection_name/generation/count，须避免在整库激活之前改当前指针。目标代次coverage以真实points为准，进度留在build记录或独立报告。
- prepare_generation在含远端建collection的同一事务内推进counter；若远端建成后超时/DB回滚，会重用孤立collection名称。需先持久化保留编号/登记building，再建外部collection，失败登记failed，下一次分配新编号。应加“创建成功后抛异常”的回归。

以上不是追加用户需求，均是已声明失败安全、重试、任务互斥、模型切换要求的默认路径核验。

## 2026-10-14 用户恢复开发后的复核

用户先明确暂停并保存进度，后明确“继续”，现已恢复。DeepseekHarness 仍负责实现与测试代码，Codex 独立审查。

- 启动已安装的 Docker Desktop 并恢复项目三个服务，均 healthy；模型调用显式禁用，未探测 AS711。
- 独立 SQLite 网页/上传专项：18 passed（tests/test_webui.py、tests/test_upload_api.py）。仅本地 Qdrant payload index 警告。
- 浏览器使用既有验收账号与 codex-acceptance 样本：单选信息库出现，离线关键词检索真实命中1条；folder-sample.txt 的 JSON 重试不再415，分段/向量显示由0/0变为1/0。模型禁用导致后续处理失败，不能算向量入库成功。
- 发现检索下拉缺“全部信息库”、重试将离线失败误报可能忙碌、parse_error未按角色控制且直接显示技术文案，已反馈修复；尚待再验。
- 真实worker测试原有real_broker标记绕过默认dispatch拦截，上传API会先发业务queue；命名空间虽生成但父进程未应用，可能与业务集合碰撞。已要求所有测试拦截默认派发，父子共享随机集合前缀与Redis DB15随机队列，精确清理自己的集合/结果键。隔离确认前不独立运行该专项。
- worker子进程环境被.env override覆盖，且embedding.profiles优先读旧环境端点；要求fail-closed测试settings、最终活动profile只指本地桩、禁止AS711。业务队列不得清空。
- claim原实现先把DONE覆盖RUNNING，再检查DONE，检查无效；预算检查在claim后抛错留下RUNNING。已要求条件CAS并覆盖旧实例、预算用尽回归；重建复用只读分段、不消耗普通重试预算、激活前不改变文件活动指针。

本节记录审查时点，最新修复仍在进行，不代表当前全量通过或P2已验收。恢复后实际完整测试结果另行补记。

### 恢复后的全量独立复测

- PostgreSQL 独立测试库 `KB_PG_TEST_DB_NAME=kb_codex_review_20261014`，显式 `KB_PYTEST_DB=postgres`，启用 `--kb-qdrant --kb-broker`：**241 passed、1 skipped，52.69 秒**。唯一跳过 AS711 实机联调。
- 本次已包含真正独立 Celery worker 消费、同 task SUCCESS/ok、数据库 DONE、实际测试向量点与重复派发，以及缺失/错误内容绑定拒绝。模型外呼仅本机隔离桩，不能据此宣称 AS711 模型效果已通过。
- 前一轮该专项曾真实捕获 `NameError: rebuilding is not defined`，修复后本次全量通过；失败轮日志也确认 Redis DB15、随机测试集合、准确删除本次集合。
- P2 后端验收范围通过。网页缓存失效、全部信息库选项、诊断按角色隐藏/折叠仍在收尾；已要求实现方收尾后直接开始 P3 首页 AI 会话与操作。

### 用户调整开发顺序（最新要求优先）

用户明确告知 AS711 已恢复连接，并要求“先不进行功能测试了，先开发系统，完成后再测”。已即时通知 DeepseekHarness 停止继续功能/worker/浏览器测试，直接开发 P3–P7；Codex 同样停止功能测试，仅进行代码审查、进度协调和必要静态核对。测试代码可随实现编写但暂不执行。模型服务恢复尚未重新探测或联调，不能据用户告知写成验证通过。后续开发完成后统一测试，前述241项结果只对应此前代码快照。

### P3–P7 开发中的静态审查（未做功能验收）

当前已新增真实 LLM 客户端、会话/操作服务与API、首页助手及右侧派生文档、一次性确认/恢复凭证、受控删除授权与清理记录、来源选择、部署文档和待执行压测脚本。它们仍处于实现与修正中，不据文件存在宣称功能通过。

已交实现方的主要修正：

- library:admin不能复用writer判断；参数须验证类型/长度，最新会话消息不能被前20条历史截断；所有指定库须与授权库相交，不能静默仅搜索前5个库。
- 确认每一步对操作行加锁，凭证严格绑定用户/会话/操作/意图/对象；执行用ARMED到EXECUTING的条件更新；破坏性操作后端必须三次确认并核验当前名称与目标快照，库身份不能只绑可复用code。
- 能力目录与操作记录路由拆开；派生文档编辑移除源资料只读装饰器；确认卡独立通知区域、三次独立弹窗、错误可重试与刷新后恢复；实际操作结果必须展示/接入文档和上传流程。
- 删除分段的触发器也须根据同事务精确file授权放行，审计表仍不可删除；单文件清所有登记代次，按payload过滤而非猜ordinal；整库删除其独占集合；目标锁/活跃租约检查/审计快照/可重试清理API与页面齐备。
- 检索残留点须在重排之前及返回前按当前资料存在、哈希/库匹配、活动库与授权范围过滤，避免已删除资料出现在结果或模型输入。
- 修改已部署触发器需要新增显式刷新迁移，不能只修改已应用迁移；新增0011等应在后续统一安装/升级验收覆盖。
- 压测脚本须真实认证、真实multipart上传隔离样本、合法一次性凭证并发和安全只读操作执行，记录网络错误/成功率/吞吐/分位延迟；不允许拿未认证失败响应或仅创建提案称上传容量，也不能间接对AS711做未隔离压力测试。
- 文档Markdown须真正渲染表格/强调/代码并安全处理HTML；README与运维文档同步实际首页、新路由、用户报告AS711恢复且未复测的状态。生产配置准备恢复真实模型模式；任何容量结论待实测。

恢复功能测试的条件：剩余产品实现与文档落地、实现方提交可测试的完整版本；之后统一执行离线/PG/Qdrant/worker、浏览器功能与AS711联调，记录新的结果。用户调整顺序后，Codex尚未发起任何功能测试或AS711请求。
# 界面原型还原追加审查（2026-10-14 22:29 后，进行中）

用户要求原样采用根目录 `knowledge-base-prototype.html`，仅首页增加可收起的右侧文档栏，使用说明独立成页。旧版功能验收不代表这轮界面已通过。

- 原型八页截图及布局基准已保存在 `artifacts/prototype-reference/`，原文件未修改。
- 已退回实现方修正：以配置存在代替服务健康、恒真绿灯、失败资料当停滞任务、分页窗口当全库数量，以及擅自替换原型卡片/表格结构。
- 首页、文件管理、搜索仍须完整恢复原型 DOM，不能仅修改模板区块名称；文档栏须默认收起。
- 新金额字段须同时保持 ORM 与数据库不可变保护；数据库迁移双向和 SQLite/PostgreSQL 均待最终独立回归。
- 当前 AS711 断开，不发起模型请求或探测。

首页首轮静态复核待修项：工具弹窗内待办卡的事件绑定范围、嵌套确认弹窗必须使用自身返回的遮罩、需要参数的操作须有输入表单、执行后须即时更新卡片状态并支持打开派生文档/结果、会话发送需防重复提交。数据库实际列集合查询目前写成 PostgreSQL 专用 SQL，SQLite 回退路径也需后续修正与验证。

