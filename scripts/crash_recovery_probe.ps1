<#
.SYNOPSIS
  worker 崩溃恢复实测（隔离队列，绝不触碰主队列 kb_worker_v2 与 Redis DB0 业务消息）。

.DESCRIPTION
  背景：`docs/delivery-status-2026-10-15.md` §5.2 里「worker 崩溃恢复未实测」曾是没有
  真实证据的可靠性缺口。这个脚本把它变成可重复执行的验收：

    场景 A  杀掉 worker 的**池子进程**（父进程存活）
            → 期望 `task_reject_on_worker_lost` 立刻重投，秒级恢复；
    场景 B  **硬杀整个容器**（模拟宿主宕机 / OOM / docker kill）
            → 父进程一起没了，没人 reject，消息只能靠 kombu 的 visibility_timeout
              重新投递（kombu transport/redis.py L369 写 score=变为未确认的时刻，
              L415/L419 只在 age > visibility_timeout 后重新投递）。

  场景 B 默认用 `-VisibilityTimeout 30` 启动探针 worker，这样不必等真实默认值
  （1800+300=2100s）就能验证"机制确实按 visibility_timeout 生效"。生产默认值由
  `config/settings.py` 的 `KB_CELERY_VISIBILITY_TIMEOUT`（默认=任务硬超时+300）给出。

  证据写到 `-OutFile`（默认 artifacts/acceptance/worker-crash-recovery.json）；
  退出码 0 = 两个场景都在预算内恢复，1 = 有一个没恢复。

.EXAMPLE
  pwsh -File scripts/crash_recovery_probe.ps1
  pwsh -File scripts/crash_recovery_probe.ps1 -Count 120 -VisibilityTimeout 30

.NOTES
  为什么有 `Invoke-Docker` 这一层：Windows PowerShell 5.1 会把原生命令写到 stderr 的
  正常进度输出（"Container kb-crash-probe Creating"）当成 **NativeCommandError**；
  在 `$ErrorActionPreference = "Stop"` 下这会把脚本打断（实测踩过两次）。所以所有
  docker 调用都走这个包装：统一接管 stderr、显式检查 $LASTEXITCODE、按需喂 stdin。
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [int]$Count = 200,
    [int]$VisibilityTimeout = 30,
    [string]$Queue = "kb_crash_probe",
    [string]$ComposeFile = "deploy/docker-compose.unified.yml",
    [string]$WebContainer = "knowledgebase-web",
    [string]$ProbeContainer = "kb-crash-probe",
    [string]$OutFile = "artifacts/acceptance/worker-crash-recovery.json",
    [int]$CaseATimeoutSeconds = 180,
    [switch]$SkipCleanup
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repo

function Write-Step { param([string]$Message) Write-Host "== $Message" -ForegroundColor Cyan }
function Write-Fact { param([string]$Message) Write-Host "   $Message" }

function Invoke-Docker {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$DockerArgs,
        [string]$StdinInput,
        [switch]$AllowFailure
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # 让 stderr 变成可收集记录，而不是终止错误
    try {
        if ($PSBoundParameters.ContainsKey("StdinInput")) {
            $output = $StdinInput | & docker @DockerArgs 2>&1
        } else {
            $output = & docker @DockerArgs 2>&1
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if (-not $AllowFailure -and $code -ne 0) {
        throw "docker $($DockerArgs -join ' ') 失败（exit $code）：$output"
    }
    return [pscustomobject]@{ code = $code; output = @($output) }
}

function Invoke-Probe {
    param([string[]]$ProbeArgs)
    # 探针工具报的 visibility_timeout 必须与探针 worker 实际用的一致，否则证据里的
    # "还要等 N 秒"会和恢复时间对不上（两边共用 KB_CRASH_VISIBILITY_TIMEOUT）。
    $result = Invoke-Docker (@("exec", "-e", "PYTHONPATH=/app",
            "-e", "KB_CRASH_VISIBILITY_TIMEOUT=$VisibilityTimeout",
            $WebContainer, "python", "/tmp/crash_probe.py") + $ProbeArgs)
    $hit = $result.output | Select-String -Pattern "CRASH_PROBE " | Select-Object -First 1
    if (-not $hit) { throw "探针工具无输出（$($ProbeArgs -join ' ')）：$($result.output)" }
    return ($hit.Line -replace "^.*CRASH_PROBE ", "") | ConvertFrom-Json
}

function Start-ProbeWorker {
    param([int]$Visibility = 0)
    Invoke-Docker @("rm", "-f", $ProbeContainer) -AllowFailure | Out-Null
    # --no-deps：依赖（postgres/redis/qdrant/migrate）已经在跑，不要为了这个探针
    # 去重建/启动它们（否则会把一次性 migrate 服务也卷进来）。
    $dockerArgs = @("compose", "-f", $ComposeFile, "run", "--no-deps", "-d", "--name", $ProbeContainer)
    if ($Visibility -gt 0) { $dockerArgs += @("-e", "KB_CELERY_VISIBILITY_TIMEOUT=$Visibility") }
    # 就绪判定读日志里的 "ready."，所以必须 INFO（WARNING 不会打印它）。
    $dockerArgs += @("worker", "celery", "-A", "config", "worker", "-Q", $Queue,
        "--concurrency=1", "--loglevel=INFO")
    Invoke-Docker $dockerArgs | Out-Null
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 1
        $logs = Invoke-Docker @("logs", $ProbeContainer)
        if ($logs.output | Select-String -Pattern "ready\.") { return }
    }
    throw "探针 worker 40s 内没有 ready"
}

function Get-PoolChildPid {
    $code = @'
import os
def cmd(p):
    try:
        return open("/proc/%d/cmdline" % p, "rb").read().replace(b"\x00", b" ").decode()
    except Exception:
        return ""
def ppid(p):
    try:
        return int(open("/proc/%d/stat" % p).read().split()[3])
    except Exception:
        return -1
kids = [p for p in range(2, 400) if os.path.isdir("/proc/%d" % p) and ppid(p) == 1 and "celery" in cmd(p)]
print("POOLCHILD", kids[0] if kids else "")
'@
    $result = Invoke-Docker @("exec", "-i", $ProbeContainer, "python", "-") -StdinInput $code
    $hit = $result.output | Select-String -Pattern "POOLCHILD" | Select-Object -First 1
    if (-not $hit) { return 0 }
    $value = ($hit.Line -replace "^.*POOLCHILD\s*", "").Trim()
    if (-not $value) { return 0 }
    return [int]$value
}

function Wait-AllSuccess {
    param([string[]]$Ids, [int]$TimeoutSeconds)
    $start = Get-Date
    $last = $null
    for ($i = 0; $i -lt [int]($TimeoutSeconds / 2); $i++) {
        Start-Sleep -Seconds 2
        $last = Invoke-Probe (@("status") + $Ids)
        if ($last.states.SUCCESS -eq $Ids.Count) {
            return [pscustomobject]@{
                recovered = $true
                seconds   = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
                state     = $last
            }
        }
    }
    return [pscustomobject]@{
        recovered = $false
        seconds   = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
        state     = $last
    }
}

function Clear-ProbeLeftovers {
    $code = @'
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
import redis
from django.conf import settings
from apps.knowledge.models import PatrolRun
client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=5)
queue = os.environ.get("KB_CRASH_QUEUE", "kb_crash_probe")
queue_removed = client.delete(queue)
# 只清理**本队列**遗留的未确认消息：value 是 kombu 存的 pickle，里面含 routing_key（队列名）
unacked_removed = 0
for tag in client.zrange("unacked_index", 0, -1):
    raw = client.hget("unacked", tag)
    if raw and queue.encode() in raw:
        client.zrem("unacked_index", tag)
        client.hdel("unacked", tag)
        unacked_removed += 1
patrol_rows = PatrolRun.objects.filter(source="crash-probe").delete()[0]
print("CLEANUP " + str({"queue_removed": queue_removed,
                        "unacked_removed": unacked_removed,
                        "patrol_rows": patrol_rows}))
'@
    $result = Invoke-Docker @("exec", "-i", "-e", "PYTHONPATH=/app", "-e", "KB_CRASH_QUEUE=$Queue",
        $WebContainer, "python", "-") -StdinInput $code
    return ($result.output | Select-String -Pattern "CLEANUP" | Select-Object -First 1).Line
}

Write-Step "前置检查"
Invoke-Docker @("inspect", "--format", "{{.State.Running}}", $WebContainer) | Out-Null
Invoke-Docker @("cp", "scripts/crash_probe_celery.py", "${WebContainer}:/tmp/crash_probe.py") | Out-Null
Write-Fact "队列=$Queue（主队列 kb_worker_v2 与 Redis DB0 全程不动）"

$evidence = [ordered]@{
    measured_at = (Get-Date).ToString("s")
    queue       = $Queue
    tasks_sent  = $Count
    case_a      = $null
    case_b      = $null
}

Write-Step "场景 A：杀池子进程（父进程存活 → task_reject_on_worker_lost 应立即重投）"
Start-ProbeWorker | Out-Null
$enqueueA = Invoke-Probe @("enqueue", "$Count")
Write-Fact "投递 $($enqueueA.enqueued) 条；投递后 队列 $($enqueueA.queue_len) / 未确认 $($enqueueA.unacked_count)"
Start-Sleep -Milliseconds 500
$childPid = Get-PoolChildPid
if ($childPid -le 0) { throw "没找到池子进程，无法做场景 A" }
Invoke-Docker @("exec", $ProbeContainer, "python", "-c",
    "import os,signal,sys;os.kill(int(sys.argv[1]),9)", "$childPid") | Out-Null
Write-Fact "已 SIGKILL 池子进程 pid=$childPid"
$caseA = Wait-AllSuccess -Ids $enqueueA.ids -TimeoutSeconds $CaseATimeoutSeconds
Write-Fact "结果：$($caseA.seconds)s 内 SUCCESS=$($caseA.state.states.SUCCESS)/$Count，未确认 $($caseA.state.unacked_count)"
$evidence.case_a = [ordered]@{
    killed_child_pid = $childPid
    recovered        = $caseA.recovered
    seconds          = $caseA.seconds
    success          = $caseA.state.states.SUCCESS
    states           = $caseA.state.states
}

Write-Step "场景 B：硬杀整个容器（模拟宿主宕机）→ 只能靠 visibility_timeout 重投"
Start-ProbeWorker -Visibility $VisibilityTimeout | Out-Null
$enqueueB = Invoke-Probe @("enqueue", "$Count")
Start-Sleep -Milliseconds 400
$beforeKill = Invoke-Probe (@("status") + $enqueueB.ids)
Write-Fact "硬杀前：SUCCESS=$($beforeKill.states.SUCCESS) 队列=$($beforeKill.queue_len) 未确认=$($beforeKill.unacked_count)"
Invoke-Docker @("kill", $ProbeContainer) | Out-Null
Write-Fact "已 docker kill $ProbeContainer"
Start-Sleep -Seconds 2
$unacked = Invoke-Probe @("unacked")
Write-Fact "崩溃后：队列=$($unacked.queue_len) 未确认=$($unacked.unacked_count) 探针 worker 的 visibility_timeout=$($unacked.visibility_timeout_effective)s（生产默认 $($unacked.visibility_timeout_default)s）"
$unacked.unacked_index | ForEach-Object {
    Write-Fact "  未确认 $($_.id.Substring(0, 8))… 还要等 $($_.eligible_in_seconds)s 才会重投"
}
Start-ProbeWorker -Visibility $VisibilityTimeout | Out-Null
$caseB = Wait-AllSuccess -Ids $enqueueB.ids -TimeoutSeconds ([int]($VisibilityTimeout + 150))
Write-Fact "重启后：$($caseB.seconds)s 内 SUCCESS=$($caseB.state.states.SUCCESS)/$Count，未确认 $($caseB.state.unacked_count)"
$evidence.case_b = [ordered]@{
    visibility_timeout_used = $unacked.visibility_timeout_effective
    main_default_timeout    = $unacked.visibility_timeout_default
    killed_container        = $ProbeContainer
    seconds                 = $caseB.seconds
    recovered               = $caseB.recovered
    success                 = $caseB.state.states.SUCCESS
    states                  = $caseB.state.states
    unacked_at_crash        = $unacked.unacked_index
}

Write-Step "清理（只清本队列与本次实测产生的巡检留痕）"
if (-not $SkipCleanup) {
    Invoke-Docker @("rm", "-f", $ProbeContainer) -AllowFailure | Out-Null
    Write-Fact (Clear-ProbeLeftovers)
}

$evidence.verdict = if ($evidence.case_a.recovered -and $evidence.case_b.recovered) {
    "PASS：子进程被杀 $($evidence.case_a.seconds)s 内恢复；整容器硬杀在 visibility_timeout=$VisibilityTimeout s 内恢复"
} else {
    "FAIL：场景 A=$($evidence.case_a.recovered) 场景 B=$($evidence.case_b.recovered)"
}
$evidence | ConvertTo-Json -Depth 8 | Out-File $OutFile -Encoding utf8
Write-Step $evidence.verdict
Write-Fact "证据：$OutFile"

if ($evidence.case_a.recovered -and $evidence.case_b.recovered) { exit 0 } else { exit 1 }
