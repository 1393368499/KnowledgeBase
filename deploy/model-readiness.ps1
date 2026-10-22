# 模型接入体检：宿主/容器可达性 → 真实探测（kbprobe --probe）→ 健康检查 → 结论与下一步。
# 用法：powershell -ExecutionPolicy Bypass -File deploy\model-readiness.ps1
#       -SkipProbe    只做可达性与配置检查（快）
#       -TimeoutSec N 单次探测超时（默认 20，传给 kbprobe --timeout）
# 只读：不改配置、不写业务库；kbprobe 产物写到 artifacts/acceptance/。
param(
    [switch]$SkipProbe,
    [double]$TimeoutSec = 20,
    [string]$WebFile = "deploy\docker-compose.unified.yml",
    [string]$EnvFile = ".env",
    [string]$WebContainer = "knowledgebase-web",
    [string]$WorkerContainer = "knowledgebase-worker",
    [string]$BaseUrl = "http://127.0.0.1:8800"
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Write-Head($text) { Write-Host ""; Write-Host "=== $text ===" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Bad($text)  { Write-Host "  [FAIL] $text" -ForegroundColor Red }
function Write-Info($text) { Write-Host "  ...    $text" }

function Test-Tcp([string]$Target, [int]$Port, [int]$TimeoutMs = 4000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($Target, $Port)
        if ($task.Wait($TimeoutMs) -and $client.Connected) { return $true }
        return $false
    } catch { return $false } finally { $client.Close() }
}

# ---- 1. 解析要检查的端点（.env 覆盖优先，否则用 settings.py 的默认值）----
$defaults = [ordered]@{
    "llm"       = @{ host = "172.16.100.114"; port = 10001; env = "AS711_LLM_BASE_URL" }
    "embedding" = @{ host = "172.16.100.114"; port = 8000;  env = "AS711_EMBED_BASE_URL" }
    "rerank"    = @{ host = "172.16.100.114"; port = 8002;  env = "AS711_RERANK_BASE_URL" }
    "ocr"       = @{ host = "172.16.100.114"; port = 8003;  env = "AS711_OCR_BASE_URL" }
}
$envMap = @{}
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $envMap[$Matches[1]] = $Matches[2].Trim().Trim('"')
        }
    }
    Write-Info "读入 $EnvFile（$($envMap.Count) 个变量）"
} else {
    Write-Info "没有 $EnvFile：全部使用 config/settings.py 的默认端点"
}

$targets = @()
foreach ($name in $defaults.Keys) {
    $spec = $defaults[$name]
    $host_ = $spec.host; $port_ = $spec.port; $source = "settings.py 默认值"
    $url = $envMap[$spec.env]
    if ($url) {
        try {
            $uri = [System.Uri]$url
            $host_ = $uri.Host; $port_ = $uri.Port; $source = "$EnvFile $($spec.env)"
        } catch { Write-Bad "$($spec.env) 不是合法 URL：$url（回退默认值）" }
    }
    $targets += [pscustomobject]@{ Name = $name; Host = $host_; Port = $port_; Source = $source }
}

Write-Head "配置端点"
$targets | Format-Table -AutoSize | Out-String | Write-Host

# ---- 2. 宿主侧可达性 ----
Write-Head "宿主侧 TCP 可达性"
$hostBad = @()
foreach ($t in $targets) {
    if (Test-Tcp $t.Host $t.Port) { Write-Ok "$($t.Name) $($t.Host):$($t.Port)" }
    else { Write-Bad "$($t.Name) $($t.Host):$($t.Port) 不可达"; $hostBad += $t.Name }
}

# ---- 3. 容器侧可达性（Docker 网络是独立路径）----
Write-Head "容器侧 TCP 可达性（$WebContainer）"
$containerBad = @()
$py = @'
import socket, sys
targets = [("llm", 10001), ("embedding", 8000), ("rerank", 8002), ("ocr", 8003)]
host = "172.16.100.114"
bad = []
for name, port in targets:
    try:
        with socket.create_connection((host, port), 3):
            print(f"OK   {name} {host}:{port}")
    except Exception as exc:
        print(f"FAIL {name} {host}:{port} {type(exc).__name__}: {exc}")
        bad.append(name)
print("BAD=" + ",".join(bad))
'@
$py | Out-File -Encoding ascii "$env:TEMP\_readiness_socket.py"
docker cp "$env:TEMP\_readiness_socket.py" "${WebContainer}:/tmp/_readiness_socket.py" 2>$null | Out-Null
$containerOut = docker exec "${WebContainer}" python /tmp/_readiness_socket.py 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Bad "容器内探测没跑起来（容器未运行？）：$containerOut"
    $containerBad = @("unknown")
} else {
    foreach ($line in @($containerOut)) { Write-Host "  $line" }
    $badLine = @($containerOut) | Where-Object { $_ -like "BAD=*" } | Select-Object -First 1
    if ($badLine -and $badLine.Length -gt 4) { $containerBad = $badLine.Substring(4).Split(",") }
}

# ---- 4. 真实探测（权威口径）----
Write-Head "真实模型探测：manage.py kbprobe --probe"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$probeJson = "artifacts\acceptance\model-probe-$stamp.json"
$probeOk = $false
if ($SkipProbe) {
    Write-Info "已跳过（-SkipProbe）。要判定模型是否真可用，必须跑这一步。"
} else {
    New-Item -ItemType Directory -Force -Path "artifacts\acceptance" | Out-Null
    Write-Info "真调嵌入/重排/OCR（受超时/重试/熔断约束，最长约 $([int]($TimeoutSec * 3 + 30))s）..."
    # 关键：让 kbprobe 把 JSON 写进容器内文件再拷出来 —— 直接读 stdout 会被控制台编码与
    # 多行 pretty-print 破坏（实测会把 '{"' 之类的行头拼成 `{ { { {` 而解析失败）。
    $tmpInContainer = "/tmp/kbprobe-$stamp.json"
    $raw = docker exec $WebContainer sh -c "python manage.py kbprobe --probe --json --timeout $TimeoutSec > $tmpInContainer 2>/tmp/kbprobe-stderr.txt; echo EXIT=`$?" 2>&1
    $code = ($raw | Where-Object { $_ -match '^EXIT=\d+$' } | Select-Object -First 1) -replace '^EXIT=', ''
    $jsonText = ""
    $copied = docker cp "${WebContainer}:$tmpInContainer" $probeJson 2>&1
    if ($LASTEXITCODE -eq 0 -and (Test-Path $probeJson)) {
        $jsonText = [System.IO.File]::ReadAllText((Resolve-Path $probeJson).Path, (New-Object System.Text.UTF8Encoding($false)))
    } else {
        Write-Bad "没能从容器取出 kbprobe 输出：$copied"
    }
    if ($jsonText) {
        try {
            $report = $jsonText | ConvertFrom-Json
            foreach ($r in @($report.results)) {
                $ready = $r.ready
                $extra = ""
                if ($r.service -eq "embedding") { $extra = "actual_dimension=$($r.actual_dimension)/declared=$($r.declared_dimension)" }
                elseif ($r.service -eq "rerank") { $extra = "top_index=$($r.top_index) top_score=$($r.top_score)" }
                if ($ready) { Write-Ok "$($r.service) ready  $extra" }
                else { Write-Bad "$($r.service) NOT ready  error=$($r.error)"; $probeOk = $false }
            }
            $probeOk = -not (@($report.results) | Where-Object { -not $_.ready })
            Write-Info "产物：$probeJson"
            foreach ($note in @($report.environment_warnings)) { Write-Info "环境提示：$note" }
        } catch {
            Write-Bad "kbprobe 输出不是可解析 JSON（exit=$code）：$($jsonText.Substring(0, [Math]::Min(200, $jsonText.Length)))"
        }
    } else {
        Write-Bad "kbprobe 没输出 JSON（exit=$code）：$raw"
    }
    Write-Info "容器内 `kbprobe` 会把熔断快照一并打印；熔断状态是进程内的，重启容器即复位。"
}

# ---- 5. 健康检查 + 队列 ----
Write-Head "服务健康"
try {
    $health = Invoke-WebRequest -Uri "$BaseUrl/api/v1/health/ready/" -TimeoutSec 10 -UseBasicParsing
    Write-Ok "$BaseUrl/api/v1/health/ready/ HTTP $($health.StatusCode)"
    $health.Content | Write-Host
} catch {
    Write-Bad "$BaseUrl/api/v1/health/ready/ 不可用：$($_.Exception.Message)"
}
$queues = docker exec $WorkerContainer celery -A config inspect active_queues 2>&1 | Select-String -Pattern 'kb_worker_v2|name' | Select-Object -First 4
if ($queues) { $queues | ForEach-Object { Write-Info "worker 队列：$_" } }
else { Write-Bad "worker 没有报告 kb_worker_v2 队列（检查 deploy/docker-compose.unified.yml:62 与宿主 .env 的 KB_CELERY_QUEUE 是否一致）" }

# ---- 6. 结论 ----
Write-Head "结论"
$modelOk = if ($SkipProbe) { "未判定（跳过了真实探测）" } elseif ($probeOk) { "四路模型真实调用通过" } else { "模型目前不可用" }
Write-Host "  网络（宿主）："  -NoNewline
if ($hostBad.Count -eq 0) { Write-Host "通" -ForegroundColor Green } else { Write-Host "不通 -> $($hostBad -join ', ')" -ForegroundColor Red }
Write-Host "  网络（容器）："  -NoNewline
if ($containerBad.Count -eq 0) { Write-Host "通" -ForegroundColor Green } else { Write-Host "不通 -> $($containerBad -join ', ')" -ForegroundColor Red }
Write-Host "  模型：" -NoNewline
if ($modelOk -like "四路*") { Write-Host $modelOk -ForegroundColor Green } else { Write-Host $modelOk -ForegroundColor Yellow }
Write-Host ""
Write-Host "下一步：" -ForegroundColor Cyan
if ($hostBad.Count -gt 0 -or $containerBad.Count -gt 0) {
    Write-Host "  1) 先解决网络：宿主与容器都要能连到上面的端点（容器路径独立于宿主路径）。"
} elseif ($SkipProbe) {
    Write-Host "  1) 跑一次真实探测：powershell -ExecutionPolicy Bypass -File deploy\model-readiness.ps1"
} elseif (-not $probeOk) {
    Write-Host "  1) 端点通了但真实调用失败：看上面的 error 与 kbprobe 的熔断快照；确认模型名/维度（AS711_EMBED_DIM=1024）与端点实现一致。"
    Write-Host "  2) 熔断 60s 自动半开；要立刻复位就重建容器：docker compose -f $WebFile --env-file $EnvFile up -d --force-recreate web worker scheduler"
} else {
    Write-Host "  1) 界面「系统配置 -> 测试连通」应显示已连通（注意：重排/OCR 的界面探测只 GET /v1/models，权威判定以本次 kbprobe 为准）。"
    Write-Host "  2) 端到端冒烟：上传 .txt -> 等入库完成 -> 搜索页检索 -> 助手页提问并带来源。"
}
Write-Host "  手册：docs\model-onboarding-runbook.md"
