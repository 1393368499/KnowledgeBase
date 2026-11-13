# 智库安全整改与差异复审记录

范围为 2026-11-12 审计的第 1 至 3 项，本机 Docker 为测试部署。代码基线 `e9f92f6`。变更覆盖 PostgreSQL 连接权限、Django 管理认证、Web 与语音的传输入口，以及 CI。备份与恢复按本轮范围暂缓。

| 控制 | 本轮实施与核验 |
| --- | --- |
| 数据库安全与最小权限 | PostgreSQL 升至 17.11，Docker Official Image 的 Amazon ECR 镜像按摘要固定。迁移服务使用数据库所有者。Web、Worker、Scheduler 和语音 Worker 使用独立的 `kb_runtime` 口令。隔离环境验证运行账号无超级用户、建库、建角色、公共模式建表或删除审计记录权限，并能正常创建用户和追加审计。六个数据库保护触发器保留。 |
| 后台 MFA | Django `/admin/` 复用 `django-otp` 1.7.5 的 `OTPAdminSite` 和 TOTP 设备。通过本地可信终端登记，验证设备代码后保存。回归覆盖普通会话和仅密码访问受阻、有效 TOTP 登录成功、验证码重放受阻。此控制覆盖 Django 管理站点。 |
| Web HTTPS | Caddy 2.11.9 在本机回环终止 TLS，后端 Web 使用 Docker 内部端口。受控反代传递 HTTPS 状态，Django 启用 Secure Cookie 和 HSTS。隔离环境使用导出的 CA 验证证书和就绪接口，管理登录页返回 Secure CSRF Cookie。语音网关保留设备路由白名单。 |
| CI 与复审 | Actions 按提交摘要固定，工作流仅有 `contents: read`，包含 SQLite 和 PostgreSQL 全量回归、Bandit、依赖审计、Gitleaks、迁移检查及严格部署检查。当前仓库缺少远端和对应的托管 CI 运行记录。当前记录由实现者完成，独立复核尚待具备资格的复核方签署。 |

## 适用 ASVS 5.0.0 控制

| 范围 | 证据与边界 |
| --- | --- |
| V6 认证、V7 会话 | Django 管理站点要求密码和已登记的 TOTP。使用登录和验证码重放测试验证。 |
| V8 授权、13.2.2 服务账号最小权限 | 迁移与运行身份分离。核验运行账号的数据库权限、业务写入与审计追加。 |
| V12 安全通信 | 使用本机 CA 实测浏览器入口的 HTTPS 和 Secure Cookie。容器内部连接依赖 Docker 网络的信任边界。 |
| V13 配置和凭据 | 运行口令通过只读文件注入。发布配置、依赖和 CI 引用有版本或摘要记录。 |

[OWASP ASVS 5.0.0](https://github.com/OWASP/ASVS/releases/tag/v5.0.0) · [PostgreSQL 17.11](https://www.postgresql.org/about/news/postgresql-186-1711-1615-1519-1424-and-19-beta-3-released-3365/) · [django-otp 1.7.5](https://pypi.org/project/django-otp/1.7.5/) · [Caddy 本机 HTTPS](https://caddyserver.com/docs/automatic-https#local-https)

## 验证记录

- SQLite 全量 1695 通过、51 跳过。PostgreSQL 17.11 全量 1718 通过、28 跳过。PostgreSQL 首轮发现测试 worker 继承数据库引擎的问题，修复后重跑通过。
- Ruff、Bandit 高严重度且高置信度检查、严格 Django 部署检查和迁移定义检查通过。OSV 批量核对 74 个固定 Python 依赖版本，无命中。Gitleaks 本地检查 19 个 Git 提交，无命中。
- 应用候选 `sha256:9ceccc4da1e052e6dd270e99d7f33d5e2fd39a5f14f7ed561e504fd5dcc22dba`，语音网关候选 `sha256:df7984963993e3835e0f64f406d6846323fd01f3278468ceabfd0f76ff7c6994`。构建使用当前整改工作树。现场与回归日志在 `artifacts/security-123`。

本机管理账号首次使用 `/admin/` 前需执行 `docker compose exec web python manage.py enroll_admin_totp 用户名`。用个人验证器登记并在终端确认。Caddy 根证书可按部署文档导出给测试浏览器。

本机测试部署已切换到上述应用和语音网关制品，PostgreSQL 17.11 继续使用既有命名卷。TOTP 三项迁移执行成功，六个数据库触发器在位。现场验证 HTTPS 就绪接口返回 200、严格部署检查零警告、资料库 API 对匿名访问返回 401。语音网关拒绝管理路由，对无效设备凭据返回 401。三个现有 Django 管理账号待本人登记验证器。
