from __future__ import annotations

import csv
import glob
import sqlite3
from datetime import datetime
from pathlib import Path


def pick_manifest(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    files = sorted(Path(p) for p in glob.glob(r"raw\batch_*\manifest\manifest.csv"))
    if not files:
        raise FileNotFoundError("No manifest.csv found under raw/batch_*/manifest/")
    return files[-1]


def infer_batch_dir(manifest: Path) -> Path:
    return manifest.parent.parent


def infer_batch_id(manifest: Path) -> str:
    return infer_batch_dir(manifest).name


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_summary(manifest_dir: Path) -> dict[str, str]:
    summary_path = manifest_dir / "manifest_summary.txt"
    values: dict[str, str] = {}
    if not summary_path.exists():
        return values
    for line in summary_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def now_ts() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def connect_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn
