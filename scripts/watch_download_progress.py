from __future__ import annotations

import argparse
import csv
import glob
import time
from datetime import datetime
from pathlib import Path


def pick_manifest(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    files = sorted(Path(p) for p in glob.glob(r"raw\batch_*\manifest\manifest.csv"))
    if not files:
        raise FileNotFoundError("No manifest.csv found under raw/batch_*/manifest/")
    return files[-1]


def total_rows(manifest: Path) -> int:
    with manifest.open("r", encoding="utf-8-sig", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def count_downloaded(txt_root: Path) -> tuple[int, str]:
    count = 0
    latest_ts = 0.0
    latest_path = ""
    for path in txt_root.rglob("*.txt"):
        count += 1
        ts = path.stat().st_mtime
        if ts > latest_ts:
            latest_ts = ts
            latest_path = str(path)
    latest = ""
    if latest_ts:
        latest = datetime.fromtimestamp(latest_ts).isoformat(sep=" ", timespec="seconds")
    return count, f"{latest}|{latest_path}"


def write_latest_status(
    logs_dir: Path,
    downloaded: int,
    total: int,
    percent: float,
    latest_time: str,
    latest_path: str,
) -> None:
    lines = [
        f"updated_at: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
        f"downloaded: {downloaded}",
        f"total: {total}",
        f"percent: {percent}%",
        f"latest_time: {latest_time}",
        f"latest_path: {latest_path}",
    ]
    (logs_dir / "latest_download_status.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", help="manifest.csv path")
    parser.add_argument("--interval-seconds", type=int, default=180)
    parser.add_argument("--iterations", type=int, default=0, help="0 means run forever")
    parser.add_argument("--log-file", help="custom log file path")
    parser.add_argument("--quiet", action="store_true", help="do not print progress to stdout")
    args = parser.parse_args()

    manifest = pick_manifest(args.manifest)
    batch_dir = manifest.parent.parent
    txt_root = batch_dir / "txt"
    logs_dir = batch_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = Path(args.log_file) if args.log_file else logs_dir / "download_progress_watch.log"
    total = total_rows(manifest)

    rounds = 0
    while True:
        rounds += 1
        downloaded, latest = count_downloaded(txt_root)
        latest_time, latest_path = ("", "")
        if latest:
            latest_time, latest_path = latest.split("|", 1)
        percent = 0.0 if total == 0 else round(downloaded * 100.0 / total, 4)
        line = (
            f"{datetime.now().isoformat(sep=' ', timespec='seconds')},"
            f"downloaded={downloaded},total={total},percent={percent},"
            f"latest_time={latest_time},latest_path={latest_path}\n"
        )
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
        write_latest_status(logs_dir, downloaded, total, percent, latest_time, latest_path)
        if not args.quiet:
            print(line, end="")

        if args.iterations and rounds >= args.iterations:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
