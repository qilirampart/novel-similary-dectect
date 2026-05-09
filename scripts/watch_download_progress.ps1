param(
    [string]$BatchDir = "raw\batch_20260428",
    [int]$IntervalSeconds = 180
)

$ErrorActionPreference = "Stop"

$manifestPath = Join-Path $BatchDir "manifest\manifest.csv"
$txtRoot = Join-Path $BatchDir "txt"
$logsDir = Join-Path $BatchDir "logs"
$logFile = Join-Path $logsDir "download_progress_watch.log"

if (-not (Test-Path $manifestPath)) {
    throw "Manifest not found: $manifestPath"
}

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

$total = ([System.IO.File]::ReadLines($manifestPath) | Measure-Object).Count - 1
if ($total -lt 0) {
    $total = 0
}

while ($true) {
    $files = @()
    if (Test-Path $txtRoot) {
        $files = Get-ChildItem -Path $txtRoot -Recurse -File -Filter *.txt
    }

    $downloaded = ($files | Measure-Object).Count
    $percent = if ($total -eq 0) { 0 } else { [Math]::Round(($downloaded * 100.0 / $total), 4) }
    $latest = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $latestTime = ""
    $latestPath = ""
    if ($latest) {
        $latestTime = $latest.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        $latestPath = $latest.FullName
    }

    $line = "{0},downloaded={1},total={2},percent={3},latest_time={4},latest_path={5}" -f `
        (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $downloaded, $total, $percent, $latestTime, $latestPath

    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Start-Sleep -Seconds $IntervalSeconds
}
