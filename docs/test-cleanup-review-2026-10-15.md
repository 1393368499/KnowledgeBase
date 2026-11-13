# 测试资源自动清理：独立审查

用户于 2026-10-15 要求“测试完成自动清理”。实现仍由 DeepseekHarness 编写，Codex 负责独立审查与运行验证。

## 范围

- 每次测试独占标识、资源登记清单、精确集合名和临时原件目录。
- 正常结束、失败和可处理的中断均清理；强制终止保留清单，确认运行者及 worker 已结束后恢复清理。
- 清理失败必须显式报告并使验收失败；保留小型报告与清理审计。
- 不按宽泛名称扫描删除历史集合，不删除共享原件，不清空当前实例队列。

## 独立基线

`artifacts/acceptance/cleanup-before-20261015.json`：当前实例 191 个集合；`data/media/raw` 共 271 个文件，已逐个记录 SHA256。包括历史测试残留，不能把这些文件都称为用户业务原件。

本次目录扫描：125 个 `kb-lt-*` 集合共 45,158,709,059 字节；其余 66 个集合共 24,844,858,796 字节。旧集合不自动归入新机制的删除范围。

## 当前验证

独立运行 `tests/test_run_cleanup.py`：**19 通过、1 跳过**，无失败。证据：`artifacts/acceptance/cleanup-unit-review.xml`。符号链接用例因当前权限无法创建链接而跳过；不能算通过。

这批用例使用假 Qdrant 客户端，仅证明模块的部分语义，不代表真实 pytest/压测已接入自动清理，也不代表强制终止后的恢复已验证。

## 待闭合

1. 真实测试与压测入口接入、真实集合和临时原件结束后清零。
2. 活跃进程/worker 防误清；只凭心跳超时不能判定进程已死，不能用 force 绕过。
3. 跨进程登记与清理互斥；仅唯一临时文件名不能避免读改写丢失。
4. 创建成功但尚未确认登记即中断的 pending 资源恢复。
5. 客户端真实 endpoint 身份与资源清单一致，Redis 限于独立测试 DB/精确键。
6. 完整退出场景、清理失败退出码与原有集合/原件不变验证。

以上均未完成之前，不宣称用户要求已经交付。

## 后续独立复验（北京时间 12:12）

- 真实 pytest 结束清理：run `ca33a864f206422db0221a6f8e416d39`，两集合均消失、raw 目录消失，原有 `codex-runtime-probe-20261015` 保留。证据 `cleanup-pytest-e2e-review.xml` 及本 run 清理报告。
- 首次 PostgreSQL 并发首上传复验抓到 Windows 只读原件删除失败；验收按要求非零退出，没有吞错。实现方随后修正为只在本 run 已验证目录内处理只读位并重试。
- 修正后 PostgreSQL 并发首上传、真实清理、单元及跨进程专项：**35 通过、1 跳过**，证据 `cleanup-concurrent-upload-final.xml`。run `33c6c639b62342c7aaab1098d5474040`。
- 真实故意失败演练：run `de5808c3bccd`，创建后写 marker，pytest 恰为退出码 1；集合已清、`cleanup-report.failed=[]`、raw/vector 目录已清、既有探针保留。脚本 `scripts/cleanup_failure_drill.py`，由 Codex 独立运行。
- 完整离线回归：**397 通过、18 跳过**，无失败，26.831 秒，证据 `cleanup-sqlite-final.xml`。run `e2abe3a3d213414a84e39b02a14717e2`。此前一次唯一失败是隔离 vector 目录尚未初始化导致 readiness 503，已修正并复验，未改成接受 503。
- 主实例只读复查：仍 191 集合、271 个 raw 文件，集合清单及 raw SHA 与当前保护基线相同。

尚未交付部分：压测入口的完整数据库/原件/服务回收、真实 worker 父子端点与命名空间接线及复验、CLI cleanup 活跃 run 保护最终负例、遗留 pending 与恢复并发的最终复验。不能把上述已通过专项扩写为所有测试入口均已自动清理。

## 后续独立复验（北京时间 12:29）

- 真实 worker **5/5 通过，无跳过**，28.365 秒，证据 `cleanup-real-worker-review.xml`。必须带 `--kb-broker` 才实际执行。隔离 PostgreSQL、Redis DB15、Qdrant 56339；run `c49ba4eca6f74894b40218d4ba480bf5` 的 raw/vector 目录已清，独立复查仅原有探针集合留存。证据 `cleanup-worker-independent.json`。这证明该测试入口的资源回收，不作为 Linux 高并发/崩溃恢复能力验收。
- 运行器生命周期空跑：run `8a40df18d01b`，专用数据库 `kb_loadtest_7aa517e6` 已回收；8011 服务由运行器停止；raw/vector 目录删除；`runner-report.residual=[]`。
- **真实 5 文件压测与完整收尾通过**：run `9af1b0c57d4e`，5 次上传均 HTTP202、客户端退出0。父运行器停止自己的服务后，清理1个集合、raw/vector目录以及数据库 `kb_loadtest_bc142228`；独立查询确认该数据库不存在。证据 `cleanup-load-independent.json`、本 run 的 `runner-report.json` 与 `load-probe.log`。上传受理不等于文件解析/嵌入完成，本项验证的是测试资源生命周期。
- 主实例仍 191 集合、271 个 raw 文件，集合清单和 raw SHA 未变；Redis DB0 的 `celery` 队列仍193条。历史测试数据没有被本机制删除。

当前剩余复验：pending 创建后强制中断的真实恢复、runner 子服务的身份与停止保护、所有测试夹具父目录按 session/run 隔离。上一节“尚未交付部分”是12:12快照，应按本节已取得证据更新。

## 12:40 恢复逻辑新增阻断

独立代码审查发现恢复分支使用 `os.kill(pid, 0)` 探测 Windows 进程。Python 3.13 官方文档明确：Windows 上除 CTRL_C_EVENT/CTRL_BREAK_EVENT 外，其他 sig 值调用 TerminateProcess；因此不能把 0 当作只读查询。依据：https://docs.python.org/3.13/library/os.html#os.kill 。该分支未由 Codex 执行，已交实现方立即修复为只读进程查询并新增真实子进程存活验证。

开发工具3080曾不可连接；Codex用原已安装的DSH启动脚本恢复，没有修改 Harness 源码或放宽插件版本限制。不能据此断言断连由上述错误导致；系统8000与三个数据库容器仍正常。所有恢复完成结论须等待这项修正后的独立复验。

另有并发恢复用例假通过问题：原断言允许两个恢复者都失败，且延时发生在释放锁之后。已要求以持锁期间的屏障制造真实重叠，并严格区分成功、锁忙与其他错误，不能把所有 CleanupError 都算锁拒绝。
