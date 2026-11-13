# Docker 交付独立验收记录（2026-10-15）

本轮限时窗口：北京时间15:49–16:49。系统已部署到 http://127.0.0.1:8000/app/ ，但尚未达到全部功能完成的目标。

## 当前部署

- 根 docker-compose.yml 是唯一入口，引用统一配置；web、worker、scheduler、PostgreSQL、Redis、Qdrant均由Docker管理。
- 用户16:16明确允许Docker数据卷，取代此前向量必须在项目根目录的约束。现役卷：knowledgebase-qdrantdata-delivery-20261015。旧data/vector_data/qdrant原样保留，独立WSL迁移路线不再使用。
- 9库、16份既有样例资料的22个已存分段重新嵌入；这是复用分段的索引重建，不是重新解析缺失原件。
- 重启Qdrant后9库的点/向量/payload摘要全部相同，带library_id过滤查询全部HTTP200。证据：docker-hour-volume-before-restart.json、docker-hour-volume-after-restart.json。
- 最终应用镜像ID：sha256:eccc5f0e7f6e86a2d16f5d68544dee89d6433ea1badae48baf4393adedd29536。三个应用容器一致；系统检查与依赖检查通过。

## 独立实测

- 浏览器真实AS711资料问答：北极星项目12个月/孙念，返回来源，无降级提示。
- 指定单份资料生成文档，编辑保存，真实下载MD与HTML；输出留在artifacts/acceptance/docker-hour-document.*。
- 上传独立样本id30，经Docker worker解析、真实嵌入、入向量库，状态DONE且分段1/1；搜索命中，并记录准确反馈。
- 样本通过三次独立确认与准确名称校验删除；向量残留0。其独占原件、反馈、临时会话16/17、派生文档12随后精确清理；原有内容未动。证据：docker-hour-upload-cleanup.json、docker-hour-model-evidence.json、docker-hour-data-after.json。
- 最终原件271文件，逐个SHA与部署前基线一致；原型文件SHA一致，管理员保持有效。Redis DB0旧celery队列保持193。
- 验收用Docker容器/数据卷、pytest数据库已清理；早期独立验收worker与其测试数据库也已回收。报告和小型输出保留。
- 完整PG/真实Qdrant/真实broker回归：docker-hour-postgres-release.xml。SQLite收尾复验：docker-hour-sqlite-cleanup-fixed.xml。跳过不算通过。
- 最终数字：PG 501通过/2跳过；SQLite 483通过/20跳过，均无失败。SQLite此前一次出现测试锁句柄清理错误，修正后全套复验通过。
- 失败测试run 07b0f592ac9947a8bc8c9bd9cb32f905的fixtures目录递归清理被自动审批策略拒绝（工具只返回blocked by policy，未给出具体理由），该残留暂保留，没有绕过执行。不能宣称历史/失败测试残留全部清零。

## 仍未通过或未完成

- 系统配置模型检测按钮：后端目标白名单已实现，但最终浏览器仍观察到多个按钮调用BGE且对应行未更新。前端targetMap标签与真实data-test-model不匹配，且事件未正确取得局部name。不能宣称四服务检测完成。
- 看门狗、AS711模型管理/切换、受控整库重置、反馈重扫执行、跨进程限流未完成。
- acks_late、reject_on_worker_lost、有限重试已接线；真正杀worker后重投递与恢复尚未验收。
- 最新搜索融合去重已修并通过定向测试；最终镜像含修复，但样本已清理，未再次完成该样本的浏览器去重复验。
- 原有id14/15/16没有原件路径；已有分段已重嵌入，不能据此声称原件可读取或重新解析。
- 本次重启一致性验证不等于长期稳定性与最大吞吐验证。未提供未经测量的并发能力承诺。

以上缺口必须继续闭合后，才能宣称每个功能、每个操作全部完成。
