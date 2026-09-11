param(
    [Parameter(Mandatory = $true)]
    [int]$EmbeddingProcessId,
    [Parameter(Mandatory = $true)]
    [string]$ProgressJson,
    [Parameter(Mandatory = $true)]
    [string]$ErrorLog,
    [Parameter(Mandatory = $true)]
    [string]$MonitorLog,
    [int]$IntervalSeconds = 900
)

$ErrorActionPreference = 'Stop'

while ($true) {
    $progress = $null
    $progressError = $null
    try {
        if (Test-Path -LiteralPath $ProgressJson) {
            $progress = Get-Content -LiteralPath $ProgressJson -Raw -Encoding UTF8 | ConvertFrom-Json
        }
    }
    catch {
        $progressError = $_.Exception.Message
    }

    $process = Get-Process -Id $EmbeddingProcessId -ErrorAction SilentlyContinue
    $entry = [ordered]@{
        checked_at_local = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        embedding_process_id = $EmbeddingProcessId
        process_running = ($null -ne $process)
        process_cpu_seconds = if ($process) { [math]::Round($process.CPU, 3) } else { $null }
        progress_status = if ($progress) { $progress.status } else { $null }
        total_rows = if ($progress) { $progress.total_rows } else { $null }
        processed_rows = if ($progress) { $progress.processed_rows } else { $null }
        processed_pct = if ($progress) { $progress.processed_pct } else { $null }
        eta_seconds = if ($progress) { $progress.eta_seconds } else { $null }
        progress_error = $progressError
        error_log_bytes = if (Test-Path -LiteralPath $ErrorLog) { (Get-Item -LiteralPath $ErrorLog).Length } else { $null }
    }
    $entry | ConvertTo-Json -Compress | Add-Content -LiteralPath $MonitorLog -Encoding UTF8

    if (-not $process) {
        break
    }
    Start-Sleep -Seconds $IntervalSeconds
}
