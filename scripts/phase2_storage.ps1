<#
**薄启动包装**（不再是执行器）

原因（独立审查）：原 PowerShell 实现存在跨平台调用/退出回退的**根本性问题**，
无法可靠修好：
  - 内层 `Die` 用 `exit`，**绕过 catch**，致命错误不回退；
  - `Start-Process -ArgumentList` 会把参数拼成字符串，`bash -lc $inner` 带空格
    无法可靠引用；
  - 实测该 Runtime **没有 `pgrep`**，PID 管理不成立；
  - 逐文件起 wsl 复制+哈希 ≈ 7 万次启动（小时级），间隔监测会冲破 5 GiB 下限。

因此真正的执行器改为 **Python CLI**：`scripts/storage_migrate.py`
（现有 `.venv` 即可；官方 `zipfile.open` / `hashlib` / `shutil.disk_usage`，无需新依赖），
Linux 侧为**一次启动的静态批处理脚本**。

本文件只做转发，**不包含任何危险逻辑**，也不再是可执行的迁移实现。
#>
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Rest
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$cli = Join-Path $root 'scripts\storage_migrate.py'

if (-not (Test-Path $python)) { Write-Host "找不到 .venv Python：$python" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $cli)) { Write-Host "找不到执行器：$cli" -ForegroundColor Red; exit 2 }

Write-Host "[薄包装] 转发到 Python 执行器：scripts/storage_migrate.py" -ForegroundColor Cyan
& $python $cli @Rest
exit $LASTEXITCODE
