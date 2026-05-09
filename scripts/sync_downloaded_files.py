from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity.sqlite3")
    parser.add_argument("--manifest", help="Path to manifest.csv")
    args = parser.parse_args()

    manifest = pick_manifest(args.manifest)
    batch_dir = manifest.parent.parent
    txt_root = batch_dir / "txt"
    if not txt_root.exists():
        raise FileNotFoundError(f"Missing txt root: {txt_root}")

    updates = []
    for path in txt_root.rglob("*.txt"):
        book_ext_id = path.parent.name
        chapter_id = int(path.stem)
        downloaded_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")
        updates.append((str(path), downloaded_at, "downloaded", book_ext_id, chapter_id))

    conn = sqlite3.connect(args.db)
    try:
        conn.executemany(
            """
            UPDATE chapters
               SET raw_file_path = ?,
                   downloaded_at = ?,
                   download_status = ?,
                   updated_at = ?
             WHERE book_ext_id = ?
               AND chapter_id = ?
            """,
            [(p, t, s, datetime.now().isoformat(sep=" ", timespec="seconds"), b, c) for p, t, s, b, c in updates],
        )
        conn.commit()
    finally:
        conn.close()

    print(f"OK synced_files={len(updates)}")


if __name__ == "__main__":
    main()
