# Codex 独立审查：尚未闭合事项

本清单区分已观察代码问题与尚待运行验证，不以实现方矩阵的勾选作为完成证据。第一阶段通过记录仍有效，但后续代码变化必须重新验收。

## 阶段2：部署

1. 原迁移脚本不可执行：Invoke-Expression、无条件清空新目录、非冷态快照、路径拼接、配置/版本校验缺失。需唯一新目录、冷态压缩备份、哈希清单及完整还原验证、监测至少5GiB余量、失败回退和重启持久性。
2. ext4独立环境已由Codex准备并验证；业务迁移尚未执行。65.2GiB实际分配与49.8GiB空闲禁止两次普通完整复制；零占比仅是抽样。
3. Docker构建缺少.dockerignore，将包含.env、data/media/raw、vector_data和.venv；不可构建当前草稿。
4. 原件实际路径data/media/raw需只读挂载并正确设置RAW_STORE_DIR。beat状态目录必须对uid10001可写，且必须有真实调度任务。
5. 需端到端映射broker/backend/队列，web producer与worker同专用RedisDB；不得清空或消费DB0旧消息。注册任务名knowledge.process_ingested_file不匹配apps.*路由。
6. requirements.txt中pywin32==312需Windows平台标记，否则Linux安装失败。Linux实际安装/运行结果才能证明依赖完整。

## 后续功能与协议边界

1. 派生文档来源校验：API逐项丢弃非数字source_file_ids，使["bad"]成为空列表后退回全库；标量/负数/空值/混合无权限/不存在ID须明确400且不调用模型。显式sources不可悄悄扩大范围。当前选中来源分支亦未与libraries交集，部分不可用来源被静默丢弃。需要严格serializer和精确来源校验。
2. 派生文档新建及sources.set未放在短事务中，关系写入失败可留下成功外观的孤立文档。模型外呼应在事务外，数据库新建+来源关联用短事务；生成中防重复点击，失败恢复。
3. 工具参数非法JSON或非对象当前变成{}。全为可选参数的工具可因此被视为有效提案；需明确拒绝，不能靠缺必填字段才偶然拒绝。未知/空name、非字典function、tool_calls错误类型也应有明确协议错误。
4. AI search_knowledge已登记customer/department/amount参数，但执行分支未传入结构化候选过滤；必须复用搜索API的服务端收敛逻辑。显式不存在信息库不得回退为全库。
5. CircuitBreaker目前进程内BREAKERS；容器worker和web多进程不共享。监控/重置不能只展示或重置当前web进程再冒充全系统状态，需验证实际跨进程行为或准确标注进程范围。
6. restart_stalled_task同步逐个调用kbprocess且返回挑出的IDs为restarted；需区分已提交、实际执行、成功/失败/租约忙。parse_attempts不能冒充“重启次数”。
7. 当前准确/不准确、学习、反馈处理、巡检、按需连通延迟、controlled整库重置仍待实现。模型本地所在机器待用户澄清；不得擅自启停AS711模型。

## 证据与状态

- 第一阶段独立PG350通过/1跳过，SQLite338通过/13跳过；真实AS711 RAG与浏览器指定来源生成、保存导出和原件不变另有证据。
- 当前清单条目是进一步验收边界，尚不能据此宣称这些负例已通过。修复/测试必须由DeepseekHarness编写，Codex负责独立复核。

## 进一步核对已登记意图（09:51）

8. add_to_library目前无条件抛错，与“每个功能都实现”不符。应评估按内容寻址复用只读原件，在目标库新增记录/索引的受控复制语义，保持源记录、归属和字节不变，不能为了“添加”直接改源library_id。服务已有按(library,sha256)幂等ingest能力。需源读权限、目标写权限和确认，缺原件/不完整状态要明确报告。
9. export_document忽略format且仅把状态改成EXPORTED，前端只打开文档栏，没有对应导出入口；需返回真实受权下载链接/格式并能通过AI操作抵达真实下载，未下载不能伪称文件已导出。
10. create_library登记level参数却创建时未使用；须验证internal/external并保存，非法值400，非管理员权限不放宽。
11. 部分矩阵引用docs/storage-migration-plan.md和docs/linux-worker-design.md当前不存在，不能作为实施或验收证据。

说明：显式不存在信息库在正常propose路径已由_target_library_ids拒绝，因此“搜索分支回退全库”不是已证实的正常路径越界；应按防御增强和直接服务调用边界评估，不冒称已复现漏洞。

12. Linux worker还需实际崩溃恢复：当前process_ingested_file捕获PipelineError后返回ok=false，不会触发Celery retry，max_retries声明本身不证明自动重试。当前未看到acks_late/reject_on_worker_lost接线。应在有限重试/租约幂等预算下设计，并用隔离任务杀worker子进程、恢复、重复投递及最终状态证据验收；不得消费DB0旧消息。

## 10:43 迁移草稿再次退回（禁止执行真实数据迁移）

- Windows路径未核对项目包含关系，Linux目标变量插入bash字符串、无前缀/符号链接校验。
- migrate在Execute开关之前mkdir；verify默认重启发行版。只读默认不成立。
- 备份仅随机20条解压长度一致，没有完整SHA清单/全条目流式解压哈希核验。
- 冷备/复制异常缺全阶段自动回退；migrate未强制已验证备份和旧服务停写。
- 每2000文件查空间可跨过数十GiB，整目录cp过程未监测余量。
- 复制只比较文件数下界，未逐文件hash；findmnt应-T实际目标而非失败回退根挂载。
- /etc/qdrant/config.yaml父目录未创建；后台setsid不能证明WSL持续运行；pkill全匹配应换受管PID身份核验。
- verify把单维[0.1]发给1024维collection，必失败且失败未作为硬失败；未比较健康基线点、向量、payload、计数，也未切换应用端点与覆盖回退。
- selftest含or true和自等式无效断言；Die退出进程无法被内部catch捕获。

queue-routing独立4通过，证据resume-queue-routing.xml；但因默认队列与专用队列相同，该结果尚不能证明真实路由规则的覆盖，应以不同default_queue作对照。移除apt后Docker独立构建已进入pip依赖安装，尚未完成。

## 11:05 协议边界与后台环境

- 独立定向回归26通过：工具协议10、队列路由5、空/截断答复及API失败11；证据resume-protocol-routing.xml。新路由正例已保留真实task_routes、仅改变default_queue，避免自填期望规则。
- 畸形工具现在有invalid_reason且服务拒绝提案，但拒绝原因仅放返回proposals；assistant payload未持久化invalid_reason/proposal rejection，前端sendOnce丢弃返回提案并renderHome。因此无正文且全被拒时可能显示空助手气泡，刷新后缺说明。需API负例证明无OperationRecord，并持久显示拒绝原因。此为代码路径审查，尚未浏览器注入复现。
- 工具表单sources标签写“来源资料ID”，compose_document实际按library_codes解释；应明确原sources为信息库编码，另加source_file_ids精确资料参数并验证，不应让一个参数混用两种ID域。
- Linux镜像构建/依赖/非root/文件隔离检查通过。已创建独立PG kb_codex_linux_20261015_1053并迁移，RedisDB13专用queue codex_linux_20261015_1053，容器kb-codex-linux-worker-20261015-v2真正prefork4，ping与health通过。首次独立启动因验收env遗漏DJANGO_SETTINGS_MODULE而退出，已修正验收env后新容器复验；compose本身已设置该值，不能冒称产品因此有bug。
- Linux任务执行、实际并发、崩溃恢复尚未验证，进程启动不代表通过。模型开关禁用、beat关闭；主RedisDB0队列193条保持原样。
- 源资料阶段2基线：16资料、21分段、13现存原件，实际哈希0不一致，另3条历史记录未有原件路径。source-baseline-phase2-20261015.json。
