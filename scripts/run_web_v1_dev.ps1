$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$localEnvScript = Join-Path $root ".env.local.ps1"

if (Test-Path $localEnvScript) {
  . $localEnvScript
}

$apiProcessPattern = 'python(.exe)?\s+-m\s+uvicorn\s+api\.app:app'
$workerProcessPattern = 'python(.exe)?.*run_task_worker_v1\.py'

Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -match $apiProcessPattern -or $_.CommandLine -match $workerProcessPattern
  } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }

$env:NOVEL_SIMILARITY_AUTOSTART_WORKER = '1'
$env:NOVEL_SIMILARITY_AUTOWORKER_POLL_SECONDS = '1.5'
$env:NOVEL_SIMILARITY_TASK_WORKER_NAME = 'local-worker-8001'

Start-Process -FilePath "python" -WorkingDirectory $root -ArgumentList @(
  "-m",
  "uvicorn",
  "api.app:app",
  "--host",
  "127.0.0.1",
  "--port",
  "8001"
)

Start-Process -FilePath "npm.cmd" -WorkingDirectory (Join-Path $root "web") -ArgumentList @(
  "run",
  "dev"
)
