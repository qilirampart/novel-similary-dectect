from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class QueueItem:
    key: str
    label: str
    manifest: Path
    workers: int
    retries: int
    timeout: int

    @property
    def batch_dir(self) -> Path:
        return self.manifest.parent.parent

    @property
    def txt_root(self) -> Path:
        return self.batch_dir / "txt"

    @property
    def logs_dir(self) -> Path:
        return self.batch_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.logs_dir / "download.lock"

    @property
    def out_log(self) -> Path:
        return self.logs_dir / "full_download_queue.out.log"

    @property
    def err_log(self) -> Path:
        return self.logs_dir / "full_download_queue.err.log"

    @property
    def latest_status_file(self) -> Path:
        return self.logs_dir / "latest_download_status.txt"

    @property
    def terminal_failures_file(self) -> Path:
        return self.logs_dir / "terminal_failures.csv"


def load_queue(path: Path) -> list[QueueItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items: list[QueueItem] = []
    for row in payload["batches"]:
        items.append(
            QueueItem(
                key=row["key"],
                label=row["label"],
                manifest=Path(row["manifest"]),
                workers=int(row.get("workers", 32)),
                retries=int(row.get("retries", 2)),
                timeout=int(row.get("timeout", 60)),
            )
        )
    return items


def total_rows(manifest: Path) -> int:
    with manifest.open("r", encoding="utf-8-sig", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def terminal_failures_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def count_downloaded(txt_root: Path, batch_dir: Path) -> tuple[int, str, str]:
    count = 0
    latest_ts = 0.0
    latest_path = ""
    if not txt_root.exists():
        return 0, "", ""
    for path in txt_root.rglob("*.txt"):
        count += 1
        ts = path.stat().st_mtime
        if ts > latest_ts:
            latest_ts = ts
            latest_path = path.relative_to(batch_dir).as_posix()
    latest_time = ""
    if latest_ts:
        latest_time = datetime.fromtimestamp(latest_ts).isoformat(sep=" ", timespec="seconds")
    return count, latest_time, latest_path


def write_batch_status(item: QueueItem, downloaded: int, total: int, latest_time: str, latest_path: str) -> None:
    item.logs_dir.mkdir(parents=True, exist_ok=True)
    percent = 0.0 if total == 0 else round(downloaded * 100.0 / total, 4)
    lines = [
        f"updated_at: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
        f"downloaded: {downloaded}",
        f"total: {total}",
        f"percent: {percent}%",
        f"latest_time: {latest_time}",
        f"latest_path: {latest_path}",
        f"dataset_key: {item.key}",
        f"dataset_label: {item.label}",
    ]
    item.latest_status_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_queue_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(sep=' ', timespec='seconds')} {message}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def write_queue_status(path: Path, current: QueueItem | None, downloaded: int, total: int, latest_time: str, latest_path: str) -> None:
    if current is None:
        text = [
            f"updated_at: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
            "state: all_done",
        ]
    else:
        percent = 0.0 if total == 0 else round(downloaded * 100.0 / total, 4)
        text = [
            f"updated_at: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
            "state: running",
            f"current_key: {current.key}",
            f"current_label: {current.label}",
            f"current_manifest: {current.manifest.as_posix()}",
            f"downloaded: {downloaded}",
            f"total: {total}",
            f"percent: {percent}%",
            f"latest_time: {latest_time}",
            f"latest_path: {latest_path}",
        ]
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def start_downloader(item: QueueItem, workdir: Path) -> int:
    item.logs_dir.mkdir(parents=True, exist_ok=True)
    out = item.out_log.open("a", encoding="utf-8")
    err = item.err_log.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        "scripts/download_raw_txt.py",
        "--manifest",
        str(item.manifest),
        "--workers",
        str(item.workers),
        "--retries",
        str(item.retries),
        "--timeout",
        str(item.timeout),
    ]
    try:
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=out, stderr=err)
        return proc.pid
    finally:
        out.close()
        err.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-file", required=True)
    parser.add_argument("--poll-seconds", type=int, default=180)
    args = parser.parse_args()

    workdir = Path.cwd()
    queue_file = Path(args.queue_file)
    items = load_queue(queue_file)
    top_logs = Path("raw") / "queue_logs"
    queue_log = top_logs / "download_queue.log"
    queue_status = top_logs / "download_queue_status.txt"

    append_queue_log(queue_log, f"queue_start queue_file={queue_file.as_posix()} batches={len(items)}")

    for item in items:
        total = total_rows(item.manifest)
        append_queue_log(queue_log, f"batch_enter key={item.key} label={item.label} total={total}")
        while True:
            downloaded, latest_time, latest_path = count_downloaded(item.txt_root, item.batch_dir)
            failed = terminal_failures_count(item.terminal_failures_file)
            write_batch_status(item, downloaded, total, latest_time, latest_path)
            write_queue_status(queue_status, item, downloaded, total, latest_time, latest_path)
            if downloaded + failed >= total:
                append_queue_log(
                    queue_log,
                    f"batch_complete key={item.key} downloaded={downloaded} failed={failed} total={total}",
                )
                break

            if not item.lock_path.exists():
                pid = start_downloader(item, workdir)
                append_queue_log(queue_log, f"downloader_started key={item.key} pid={pid} workers={item.workers}")

            time.sleep(args.poll_seconds)

    write_queue_status(queue_status, None, 0, 0, "", "")
    append_queue_log(queue_log, "queue_done")


if __name__ == "__main__":
    main()
