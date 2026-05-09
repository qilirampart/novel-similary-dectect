param(
    [string]$ProgressJson = "logs/backfill_gitee_chunks_qwen3_8b_1024_v1_batch001_progress.json",
    [string]$Collection = "novel_semantic_chunk_embeddings_qwen3_8b_1024_v1",
    [string]$DbPath = "data/novel_similarity_v2.sqlite3",
    [string]$OutLog = "logs/backfill_progress_10min_report.log",
    [int]$IntervalSeconds = 600
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

function Get-SyncCount {
    param([string]$DbPath, [string]$Collection)
    $safeDbPath = $DbPath.Replace("\", "\\").Replace("'", "\\'")
    $safeCollection = $Collection.Replace("\", "\\").Replace("'", "\\'")
    $code = "import sqlite3; conn = sqlite3.connect(r'$safeDbPath'); cur = conn.cursor(); sql = 'select count(*) from semantic_chunk_embedding_sync_state where collection_name=?'; cur.execute(sql, (r'$safeCollection',)); print(cur.fetchone()[0]); conn.close()"
    return [int](python -c $code)
}

while ($true) {
    $now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $progress = Read-JsonFile -Path $ProgressJson
    $syncCount = Get-SyncCount -DbPath $DbPath -Collection $Collection

    if ($null -eq $progress) {
        $line = "[${now}] progress_json_missing sync_count=$syncCount"
    } else {
        $line = "[${now}] status=$($progress.status) processed_rows=$($progress.processed_rows) total_rows=$($progress.total_rows) success_rows=$($progress.success_rows) failed_rows=$($progress.failed_rows) failed_jobs=$($progress.failed_jobs) vps=$($progress.vectors_per_second) sync_count=$syncCount"
    }

    Add-Content -LiteralPath $OutLog -Value $line -Encoding UTF8
    Start-Sleep -Seconds $IntervalSeconds
}
