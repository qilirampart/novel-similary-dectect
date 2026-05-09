param(
    [string]$ProgressJson = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch001_progress.json",
    [string]$FailuresJsonl = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch001_failures.jsonl",
    [string]$NextBatchName = "batch002",
    [int]$NextBatchLimit = 100000,
    [int]$BatchSize = 24,
    [int]$Concurrency = 12,
    [string]$Collection = "novel_semantic_chunk_embeddings_qwen3_8b_1024_v1",
    [string]$StdoutLog = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch002_stdout.log",
    [string]$StderrLog = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch002_stderr.log",
    [string]$NextProgressJson = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch002_progress.json",
    [string]$NextFailuresJsonl = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch002_failures.jsonl",
    [int]$PollSeconds = 15
)

$ErrorActionPreference = "Stop"

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return $raw | ConvertFrom-Json
}

function Has-Failures {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path
    return $item.Length -gt 0
}

$giteeToken = $env:GITEE_AI_TOKEN
if ([string]::IsNullOrWhiteSpace($giteeToken)) {
    throw "Missing GITEE_AI_TOKEN in watcher environment."
}

while ($true) {
    $progress = Read-JsonFile -Path $ProgressJson
    if ($null -eq $progress) {
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    $status = [string]$progress.status
    $failedJobs = [int]$progress.failed_jobs

    if ($status -eq "completed" -and $failedJobs -eq 0 -and -not (Has-Failures -Path $FailuresJsonl)) {
        if (Test-Path -LiteralPath $StdoutLog) {
            Remove-Item -LiteralPath $StdoutLog -Force
        }
        if (Test-Path -LiteralPath $StderrLog) {
            Remove-Item -LiteralPath $StderrLog -Force
        }

        [Environment]::SetEnvironmentVariable("GITEE_AI_TOKEN", $giteeToken, "Process")

        $args = @(
            "--canonical-only",
            "--limit", "$NextBatchLimit",
            "--batch-size", "$BatchSize",
            "--concurrency", "$Concurrency",
            "--collection", "$Collection",
            "--progress-json", "$NextProgressJson",
            "--failures-jsonl", "$NextFailuresJsonl"
        )

        $started = Start-Process `
            -FilePath python `
            -ArgumentList $args `
            -WorkingDirectory (Get-Location).Path `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog `
            -PassThru

        Write-Output ("AUTO_STARTED {0} pid={1} at {2}" -f $NextBatchName, $started.Id, (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
        break
    }

    if ($status -like "completed*" -and ($failedJobs -gt 0 -or (Has-Failures -Path $FailuresJsonl))) {
        Write-Output ("AUTO_ABORT {0} due_to_failures at {1}" -f $NextBatchName, (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
        break
    }

    Start-Sleep -Seconds $PollSeconds
}
