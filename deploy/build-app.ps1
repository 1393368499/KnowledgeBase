# 交付镜像的**唯一可复现构建入口**
#
# 用法（Windows PowerShell 5.1 与 PowerShell 7 都可直接运行）：
#   powershell -File deploy/build-app.ps1            # 优先交付路径（缓存基座），不可用则自动降级
#   pwsh       -File deploy/build-app.ps1            # 同上（装了 PowerShell 7 时）
#   ... -Standard                                    # 强制标准路径（完全从源码构建）
#   ... -Tag mytag                                   # 指定产物 tag（默认 kb-app:delivery）
#
# 部署审查纠正的三个要点（都实现在下面）：
#   1. **不能**用 `tag@sha256:<image ID>` 作为 FROM：那是本地 image ID，
#      不是 Registry manifest digest；拼上去会让 BuildKit 去 Docker Hub 解析 `kb-worker`，
#      造成长时间等待或失败。这里改为：FROM 只写 **tag**，构建前**严格校验** `.Id`。
#   2. 标准路径的第一步构建**必须立刻检查退码**：否则第一次失败后，
#      可能残留旧的 `kb-worker:local-build` tag，让第二步"成功"把失败掩盖掉。
#   3. 文件**必须存为带 BOM 的 UTF-8**：Windows PowerShell 5.1 对没有 BOM 的 .ps1
#      按系统 ANSI 代码页解码，本文件的中文注释会被解成乱码进而**直接语法报错**
#      （表现为"表达式或语句中包含意外的标记"）。改这个文件时别把 BOM 弄丢。
#   4. docker 把构建进度写在 **stderr**；一旦 stderr 被重定向（`2>&1`、CI 日志、
#      `*>&1`），`$ErrorActionPreference='Stop'` 会把"退码 0 的正常进度输出"当成
#      终止性错误（NativeCommandError）中断构建。故所有 docker 调用统一走
#      `Invoke-Docker`：把两个流都收成文本，**只**按 `$LASTEXITCODE` 判成败。
param(
    [switch]$Standard,
    [string]$Tag = "kb-app:delivery"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $baseTag = "kb-worker:phase2-review-20261015"
    # 期望的**本地 image ID**（不是 manifest digest —— 见 Dockerfile 顶部说明）
    $expectedId = "sha256:63c6532ef74939dee3bb9ceca4abfdc5639ffbcf454c3568774096ba9e4bdea8"

    # 统一的 docker 调用包装（见文件头第 4 条）：返回退码与合并后的输出文本。
    function Invoke-Docker([string[]]$DockerArgs) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $lines = & docker @DockerArgs 2>&1 | ForEach-Object { "$_" }
            $code = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
        return [pscustomobject]@{ Code = $code; Lines = @($lines) }
    }

    function Invoke-Build([string[]]$BuildArgs) {
        $result = Invoke-Docker $BuildArgs
        if ($result.Code -ne 0) {
            # 把 docker 自己的报错尾部带出来：只有退码的异常对排查毫无帮助
            $tail = ($result.Lines | Select-Object -Last 15) -join "`n"
            throw ("docker build 失败（退码 $($result.Code)）：docker $($BuildArgs -join ' ')`n" +
                   "--- docker 输出尾部 ---`n$tail")
        }
    }

    $usePinned = $false
    if (-not $Standard) {
        $probe = Invoke-Docker @("image", "inspect", $baseTag)
        if ($probe.Code -eq 0) {
            $inspect = Invoke-Docker @("image", "inspect", $baseTag, "--format", "{{.Id}}")
            $actualId = ($inspect.Lines | Select-Object -First 1).Trim()
            if ($actualId -ne $expectedId) {
                throw ("基础镜像 ID 与预期不符，拒绝构建以免用错基座。`n" +
                       "  镜像     : $baseTag`n" +
                       "  实际 ID  : $actualId`n" +
                       "  期望 ID  : $expectedId`n" +
                       "如这是有意的（例如基座已更新），请更新 deploy/app/Dockerfile 与脚本里的期望 ID；" +
                       "或改用 -Standard 走完全从源码构建。")
            }
            Write-Host "[交付路径] 基座 $baseTag 本地 image ID 校验通过：$actualId"
            $usePinned = $true
        } else {
            Write-Host "[降级] 本地没有基座镜像 $baseTag → 改用标准路径（完全从源码构建）"
        }
    } else {
        Write-Host "[标准路径] 完全从源码构建"
    }

    if ($usePinned) {
        Invoke-Build @("build", "-f", "deploy/app/Dockerfile", "-t", $Tag, ".")
    } else {
        # 第一步：从源码构建 worker 基座。**构建后立即检查退码**，
        # 不允许第一次失败后靠旧 tag 让第二次"成功"。
        Invoke-Build @("build", "-f", "deploy/worker/Dockerfile", "-t", "kb-worker:local-build", ".")
        Invoke-Build @("build", "-f", "deploy/app/Dockerfile",
                       "--build-arg", "BASE_IMAGE=kb-worker:local-build", "-t", $Tag, ".")
    }

    Write-Host "[完成] 产物 = $Tag"
    Write-Host "[下一步] docker compose up -d web worker scheduler   # 项目根即统一入口"
} finally {
    Pop-Location
}
