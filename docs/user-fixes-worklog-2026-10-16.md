# 用户本轮修复工作记录（进行中，非交付证明）

用户要求：删除 AS711 模型启动/切换及“重置数据库”，处理上一轮其余所有缺口、缺陷，完成后给登录链接。保留原型 HTML/CSS、原件、既有 admin；全部 Docker；不启停 AS711 其他服务、不消费 Redis DB0 旧 celery 队列。

## 协作与当前环境

- DeepSeekHarness 在浏览器 `http://127.0.0.1:3080/` 执行产品代码与可复跑测试修改；Codex 独立审查、诊断、验证与部署。
- Harness 原对话“知识库 AI 会话原型方案讨论”，当前同一对话目标自动继续中。不要创建新任务，不要因它一次回报未完成就结束本任务。2026-10-16 09:23 进入第149轮；最后反馈已即时发送。
- 根目录没有 Git，也未找到 AGENTS.md。
- 交付入口实际为 `http://127.0.0.1:8800/app/login/`，旧 8000/login 不应作为交付链接。必须最终用 CUA 实际登录现有 admin 验证。
- 原应用镜像为 `sha256:1a943e6b1abacea626ed01d7a49b54738e520805a943cad5f2a4b01d756a89d6`。Harness 已在本轮自行构建、显式迁移并重新部署一版，新版本还不能称完整交付，源码继续变动后必须再次统一构建部署。
- `kb-app:review-20261016` 已由 Codex 构建成功，含 LibreOffice 25.2、CJK 字体，约1.47GB。镜像初始源码并非最终版，独立测试用源码只读绑定。最终须重新构建当前源码。

## 原件保护与独占资源

- `artifacts/acceptance/user-fixes-baseline-20261016.json`：277 份原件的完整 SHA256，以及原型 HTML/CSS 哈希。最终比较既有原件不得变化。
- `artifacts/acceptance/pre-user-fixes-20261016.pgdump`：本轮前数据库备份，176023字节。
- Codex 独占 Docker 网络 `kb-audit-4e8ae96e`，三个临时容器：`kb-audit-pg-4e8ae96e`、`kb-audit-redis-4e8ae96e`、`kb-audit-qdrant-4e8ae96e`。网络别名 pg/redis/qdrant；均使用 tmpfs，无生产卷或发布端口。完成后按精确名字移除容器、网络，保留小型证据。
- PG 用户 kb，初始库 kb_audit，内部5432，隔离网络内 trust；Qdrant http://qdrant:6333；Redis redis://redis:6379/5（共享熔断测试）、/7（真实 worker测试）。不要把这些连接错指生产。
- 资源记录：`artifacts/acceptance/independent-final-resources-20261016.json`。

## 已修改并独立验证过的部分

- 删除入口源码已实施；Harness 声称当前8800浏览器16项验证通过，根仍需最终页面核查。
- 重排异常统一 degraded：独立11项专项通过，`independent-rerank-fixed-20261016.xml`。
- 恢复归档、51份重建续做：独立SQLite32项通过；独立PG+真实Qdrant36项通过，`independent-restore-continuation-pg-qdrant-20261016.log`。后续又加了owner CAS、逐份进度、归档排除等，完整最终版本需再验。
- 发现并要求修复 writer 经普通 process/retry/后台任务绕过恢复确认的问题；已加内部 allow_archived 能力，仅受控恢复可用，普通路径负例已新增。
- 共享熔断独立Linux+真实专用Redis16项通过，`independent-global-breaker-linux-20261016.log`。另发现服务索引TTL被短服务缩短（8000→600），已要求索引TTL只延长/孤儿发现/可靠重置，Harness已经修改。
- Docker APT 初期HTTP源反复500/EOF，已改官方HTTPS和有界超时；不允许整链 `|| true` 吞失败。官方HTTPS构建已完成，日志 `independent-review-build-https-20261016.log`。
- 原Office XCU使用伪键/错误路径，已依据官方LibreOffice Common.xcs要求纠正。现在禁宏/活动内容/外链的键均归Scripting，需真实外链负例通过，不能以写回自造XCU作为有效证明。
- OCR固定清晰样图与中文字体已进镜像；真实页面模型探测改走生产客户端与鉴权、代理保护。

## 当前重要待收口事项（已反馈Harness）

1. cleanup 最初仅再查询快照存在TOCTOU，后改锁内先删登记、锁外drop。**最新此方案仍不合格**：远端drop失败后登记没了，会产生不可重试孤儿集合。最后已明确要求保留退役/待删除状态或持久清理账本，activate/rollback拒绝该状态，drop成功才删除登记，失败可重试。需要真实PG两连接并发与失败后成功重试用例。
2. Office坏文件可被LO当纯文本吞下，导致伪成功，尚需修复并通过实际Linux负例。
3. Office两个真实外链测试的 `_requests_seen` 错用 `type(server.RequestHandlerClass).requests`，应读真正Handler记录；超时测试把无shebang/执行权限的.py直接Popen，在Linux失败。这些必须修测试并真的跑，不能当能力通过。
4. DOCX/PDF后端已实现；Linux真实PDF导出专项通过，前端新增两个导出按钮尚待Harness完成。
5. `tests/test_real_worker.py` 原因主机绑定的 `.venv/Scripts/python.exe` 存在而在Linux选择Windows exe；已要求统一使用 `sys.executable`，真实broker测试必须执行。
6. `artifacts/acceptance/final-ui-seed.py` 已由Harness编写，尚未执行。它应强制独占host pg、原件路径 `/app/artifacts/final-ui`，只合成资料。需要用它启动隔离UI，实际查看DOCX/PDF和归档恢复、新按钮等；不能写生产种子。
7. 最终完整回归、模型全流程、中文PDF/DOCX可视验收、原件哈希/队列/admin保护核验、统一镜像部署、真实登录与最终链接尚未全部完成。

## 正在运行/最近测试

- Codex 全套Linux真实PG/Qdrant/Redis/AS711回归：exec session **38686**，输出 `artifacts/acceptance/independent-user-fixes-full-linux-20261016.log`。配置源码RO绑定 `/app`、tmpfs `/app/artifacts` uid10001、KB_SKIP_DOTENV=1、KB_PYTEST_DB=postgres、KB_PG_HOST=pg、KB_PG_PORT=5432、KB_PG_USER=kb、KB_PG_TEST_DB_NAME=kb_audit_full_4e8ae96e、KB_TEST_QDRANT_URL=http://qdrant:6333、KB_BREAKER_STORE=process、KB_BREAKER_TEST_REDIS_URL=redis://redis:6379/5、KB_REDIS_URL=redis://redis:6379/7、KB_TEST_BROKER_DB=7、KB_TEST_AS711=1；pytest --kb-db=postgres --kb-qdrant --kb-broker -p no:cacheprovider -q。
- 最近独立Linux专项223项：216通过/3跳过/4失败，`independent-final-focused-linux-20261016.log`，四失败就是坏Office输入、两外链哨兵取错记录、假可执行超时程序无执行权限。真实PDF/DOCX成功。
- 最近宿主导出+Office163项：147通过/12跳过/4失败，`independent-export-office-20261016.xml`。三个导出断言问题已反馈，真实PDF在Windows120秒超时。宿主确有LibreOffice路径，不等于目标Docker可用。
- Codex 后续宿主全量因大量Windows转换阻塞而主动终止（session4895），**不能计完成**。只精确终止了自己带 `independent-user-fixes-sqlite-20261016.xml` 的pytest进程树；其 run `1da43c48ad9d` 已核验原件/向量目录位于独占测试根，并用run_resources cleanup成功回收。
- 原HTTP诊断容器27e461379736已终止并自动移除（纯apt临时容器）；不涉及生产服务或AS711。

## 后续验证工具

- CUA浏览器ID2，Harness tab ID1；持久句柄 `harnessTaskTab`。当前输入框索引已从510变为513，**不要复用索引**。每次先读 `getAXState({emit:false,disableDiffing:true})`，正则找“text entry area (settable) 发消息或创建任务”行取得编号，再paste/Ctrl+Return；运行时会排队，读新状态有“插话发送”按钮后立即点击。所有浏览器互动只用CUA。
- 已阅读浏览器文档；如上下文压缩后继续浏览器任务，先 `cua.rewriteDocumentation()`。只读Harness末尾可用 evaluate(document.body.innerText).slice(-2000)，不要倾倒旧历史或私人资料。
- 已应用PDF/Documents技能用于只读渲染验收并向用户说明。Poppler：`C:/Users/txy13/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe`；Documents renderer：`C:/Users/txy13/.codex/plugins/cache/openai-primary-runtime/documents/26.1013.11914/skills/documents/render_docx.py`，已读 --help。宿主LO转换会阻塞，优先用Docker实际生成的PDF再用Poppler渲染，DOCX结构可用python-docx检查。
- 用户要求完成后登录链接；别现在最终回复仅给旧链接或声称已全部完成。进度更新持续简洁中文，每60秒左右有实质更新。

## 09:58 独立验收续记
- 完整Linux首轮：1023总项，1007通过/12跳过/4失败。3失败为worker_test_settings回读.env覆盖pg:5432；1失败为review旧镜像缺pptx。报告 independent-final-full-linux-20261016.xml。不能算最终通过。
- 第二轮已启动 session82383，容器kb-final-regression2-4e8ae96e，最新delivery依赖层+源码RO，PG独占kb_final_full2_4e8ae96e，Redis5限流/熔断、7真实worker，Qdrant独占。结果 independent-final-full2-linux-20261016.log/.xml。已加KB_LIMIT_TEST_REDIS_URL、KB_TEST_BROKER_URL，前轮7个限流skip应实际执行。
- Harness已修worker显式HOST/PORT/USER/PASSWORD（空密码键必传），不回读生产.env。新增guard测试。
- 独占UI18800（kb-final-ui-4e8ae96e）曾重建自己PG数据库一次；原件tmpfs/app/artifacts/final-ui/raw，目前3md+1pdf、2草稿、1会话、1反馈。健康unhealthy源自继承worker celery ping，临时web不跑worker；生产六容器健康。
- seed先绕过索引失败；后错误POST/files/405且exit0，已修/files/upload/与失败非零、SERVER_NAME127.0.0.1、resolve路径/PGhost硬保护。最终3份合成资料全201/DONE/indexed/1024维，DOCX/PDF成功。证据 independent-final-ui-seed-retry-20261016.json。首次seed JSON含临时账号密码，最终脱敏，勿输出。
- CUA finalUiTab=tab3（18800会话），finalFilesTab=tab4（18800搜索/文件），harnessTaskTab=tab1知识库。每次发送必须核对Title。先前疑似错投经新AX/尾记录确认09:34信息实际在正确会话。
- 实际UI问答回答质保12个月、预付30%/验收后60%；指定合同来源AI写草稿成功、来源1份。截图 independent-final-ai-document-20261016.png。
- 实际浏览器下载 E:/下载/验收导出样例(合成).docx 和.pdf；结束只清理这两个自己测试文件。
- PDF通过页面上传/解析/向量化，原页预览正常；截图 independent-final-pdf-preview-20261016.png已视觉核验。导出重复标题/表格过窄已修，independent-final-export-page-1.png中文/表格/列表正常。
- UI又修：首页切模型旧承诺、Django跨行注释泄漏、tooltip实现细节、归档解析按钮、搜索重复条目。归档确认→archived→恢复确认→DONE/indexed实际成功。搜索3文件6条改为4文件4条（含新PDF）；反馈准确保存成功。
- 去重还要求保留空白词界，不能把AB C和A BC合并，Harness已收到。
- 真PG并发测试在test_cleanup_activation_lock_pg.py，根原先漏看已更正。已有3例证明PG双连接锁/retiring，但模拟SQL而非实际activate竞争；已要求补实际activate_generation竞争，之后专项。
- 最终生产kbprobe真实嵌入1024、重排0.97265625、OCR4/4关键字；independent-final-as711-probe-20261016.json。LLM由实际隔离问答/写作证明。未启停AS711服务。
- 277原件及原型HTML/CSS哈希全部一致，independent-final-original-integrity-20261016.json；主库3库0文件0反馈，admin有效staff/superuser，DB0 celery193。
- 最后待：完整回归清零、实际activate并发专项、统一构建迁移发布、现有admin真实登录、清理owned测试容器/网络/下载/临时凭据、最终报告与8800/app/login/链接。不得提前交付。
