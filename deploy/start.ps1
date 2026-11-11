# Run from any directory: powershell -File deploy/start.ps1
# Add -Voice to include the optional device/ASR services (models/tools required).
param([switch]$Voice, [switch]$SkipBuild)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    function Invoke-Docker([string[]]$DockerArgs) {
        & docker @DockerArgs
        if ($LASTEXITCODE -ne 0) { throw "Docker failed: $($DockerArgs -join ' ')" }
    }
    Invoke-Docker @("info", "--format", "{{.ServerVersion}}")
    if (-not (Test-Path -LiteralPath .env)) {
        function New-Secret {
            $bytes = New-Object byte[] 48
            $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
            try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
            return [Convert]::ToBase64String($bytes)
        }
        $sample = [IO.File]::ReadAllText((Join-Path (Get-Location) '.env.example'))
        $sample = $sample.Replace('DJANGO_SECRET_KEY=dev-only-insecure-key-change-me', "DJANGO_SECRET_KEY=$(New-Secret)")
        $sample = $sample.Replace('KB_PG_PASSWORD=kb_local_dev_only', "KB_PG_PASSWORD=$(New-Secret)")
        [IO.File]::WriteAllText((Join-Path (Get-Location) '.env'), $sample, (New-Object Text.UTF8Encoding $false))
        Write-Host 'Created .env with random signing key and database password.'
    }
    # Preserve the existing volumes and data; only create volumes that are absent.
    foreach ($volume in @('knowledgebase-pgdata', 'knowledgebase-redisdata', 'knowledgebase-qdrantdata-delivery-20261015')) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & docker volume inspect $volume --format '{{.Name}}' 2>$null
            $volumeExists = $LASTEXITCODE -eq 0
        } finally { $ErrorActionPreference = $previousPreference }
        if (-not $volumeExists) { Invoke-Docker @('volume', 'create', $volume) }
    }
    foreach ($directory in @('media/raw', 'media/templates', 'media/fonts', 'media/voice-audio', 'deploy/worker/beat-state')) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    Invoke-Docker @('compose', 'config', '--quiet')
    if (-not $SkipBuild) { & "$PSScriptRoot/build-app.ps1" }
    $composeArgs = @('compose', '-f', 'docker-compose.yml')
    if ($Voice) { $composeArgs += @('-f', 'deploy/docker-compose.voice.yml') }
    Invoke-Docker ($composeArgs + @('up', '-d', '--wait', '--wait-timeout', '240'))
    Write-Host 'Ready. Open the published web port (default http://127.0.0.1:8800/).'
    Write-Host 'First install: docker compose exec web python manage.py kbcreateuser --help'
} finally { Pop-Location }
