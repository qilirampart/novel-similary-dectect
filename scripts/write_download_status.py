from __future__ import annotations

import csv
import glob
from datetime import datetime
from pathlib import Path


def pick_manifest() -> Path:
    files = sorted(Path(p) for p in glob.glob(r"raw\batch_*\manifest\manifest.csv"))
    if not files:
        raise FileNotFoundError("No manifest.csv found under raw/batch_*/manifest/")
    return files[-1]


def total_rows(manifest: Path) -> int:
    with manifest.open("r", encoding="utf-8-sig", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def count_downloaded(txt_root: Path) -> tuple[int, Path | None]:
    latest_path: Path | None = None
    latest_ts = 0.0
    count = 0
    for path in txt_root.rglob("*.txt"):
        count += 1
        ts = path.stat().st_mtime
        if ts > latest_ts:
            latest_ts = ts
            latest_path = path
    return count, latest_path


def main() -> None:
    manifest = pick_manifest()
    batch_dir = manifest.parent.parent
    txt_root = batch_dir / "txt"
    logs_dir = batch_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    total = total_rows(manifest)
    downloaded, latest_path = count_downloaded(txt_root)
    percent = 0.0 if total == 0 else round(downloaded * 100.0 / total, 4)

    latest_time = ""
    latest_rel = ""
    if latest_path is not None:
        latest_time = datetime.fromtimestamp(latest_path.stat().st_mtime).isoformat(
            sep=" ", timespec="seconds"
        )
        latest_rel = latest_path.relative_to(batch_dir).as_posix()

    report = [
        f"updated_at: {datetime.now().isoformat(sep=' ', timespec='seconds')}",
        f"downloaded: {downloaded}",
        f"total: {total}",
        f"percent: {percent}%",
        f"latest_time: {latest_time}",
        f"latest_path: {latest_rel}",
    ]
    (logs_dir / "latest_download_status.txt").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
