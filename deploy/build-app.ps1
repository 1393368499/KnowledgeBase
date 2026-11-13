# Build knowledgebase:app from current source.
# Default: use the ID-verified local worker base when present.
# -Standard: build the self-contained Dockerfile from Python and requirements.txt.
# Compatible with Windows PowerShell 5.1; preserve the UTF-8 BOM.
# Docker progress on stderr is collected as text; only exit codes determine success.
param(
    [switch]$Standard,
    [string]$Tag = "knowledgebase:app"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $baseTag = "knowledgebase:build-base"
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
        Invoke-Build @("build", "-f", "deploy/app/Dockerfile", "--build-arg", "BASE_IMAGE=$baseTag", "-t", $Tag, ".")
    } else {
        # Dockerfile now includes the complete source-base stage.
        Invoke-Build @("build", "-f", "deploy/app/Dockerfile", "-t", $Tag, ".")
    }

    Write-Host "[完成] 产物 = $Tag"

    # 记录发布制品摘要（规则第 5 节：关键发行件记录来源与摘要）。
    # 记到 snapshots/builds/（本地证据目录，不入版本库），同时打印到控制台。
    $idInspect = Invoke-Docker @("image", "inspect", $Tag, "--format", "{{.Id}}")
    $digestInspect = Invoke-Docker @("image", "inspect", $Tag, "--format", "{{json .RepoDigests}}")
    $recordDir = Join-Path $root "snapshots\builds"
    New-Item -ItemType Directory -Force -Path $recordDir | Out-Null
    $recordPath = Join-Path $recordDir ((Get-Date).ToString("yyyyMMdd-HHmmss") + "-build.json")
    $dirtyCount = @(& git status --porcelain 2>$null).Count
    $record = [pscustomobject]@{
        tag         = $Tag
        imageId     = ($idInspect.Lines | Select-Object -First 1).Trim()
        repoDigests = (($digestInspect.Lines | Select-Object -First 1).Trim())
        gitHead     = ((& git rev-parse HEAD 2>$null) | Select-Object -First 1)
        gitDirty    = ($dirtyCount -gt 0)
        dirtyFiles  = $dirtyCount
        baseImage   = $(if ($usePinned) { "$baseTag ($expectedId)" } else { "source-base（python:3.15-slim@sha256:65a65fc7… 固定 digest）" })
        createdAt   = (Get-Date).ToString("o")
    }
    ($record | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $recordPath -Encoding UTF8
    Write-Host "[构建记录] $recordPath"
    Write-Host "[镜像摘要] $($record.imageId)"
    Write-Host "[下一步] docker compose up -d web worker scheduler   # 项目根即统一入口"
} finally {
    Pop-Location
}
