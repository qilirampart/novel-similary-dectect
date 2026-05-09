from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from v2_common import connect_db, infer_batch_dir, infer_batch_id, now_ts, pick_manifest


def read_terminal_failures(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity.sqlite3")
    parser.add_argument("--manifest", help="Path to manifest.csv")
    parser.add_argument("--dataset-key", required=True)
    args = parser.parse_args()

    manifest = pick_manifest(args.manifest)
    batch_dir = infer_batch_dir(manifest)
    batch_id = infer_batch_id(manifest)
    txt_root = batch_dir / "txt"
    logs_dir = batch_dir / "logs"
    terminal_failures_path = logs_dir / "terminal_failures.csv"

    if not txt_root.exists():
        raise FileNotFoundError(f"Missing txt root: {txt_root}")

    now = now_ts()
    updates = []
    for path in txt_root.rglob("*.txt"):
        chapter_ext_id = int(path.stem)
        stat = path.stat()
        downloaded_at = datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds")
        updates.append(
            (
                str(path),
                stat.st_size,
                downloaded_at,
                "downloaded",
                None,
                now,
                args.dataset_key,
                chapter_ext_id,
            )
        )

    failure_rows = read_terminal_failures(terminal_failures_path)
    failure_payload = []
    chapter_failure_updates = []
    for row in failure_rows:
        marked_at = row.get("marked_at") or now
        failure_payload.append(
            (
                args.dataset_key,
                int(row["chapter_id"]),
                row["book_ext_id"],
                row["status"],
                row.get("error", ""),
                row.get("raw_rel_path", ""),
                str(terminal_failures_path),
                marked_at,
                marked_at,
            )
        )
        chapter_failure_updates.append(
            (
                row["status"],
                row.get("error", ""),
                now,
                args.dataset_key,
                int(row["chapter_id"]),
            )
        )

    conn = connect_db(args.db)
    try:
        conn.executemany(
            """
            UPDATE chapters
               SET raw_file_path = ?,
                   raw_file_size = ?,
                   downloaded_at = ?,
                   download_status = ?,
                   download_error = ?,
                   updated_at = ?
             WHERE dataset_key = ?
               AND chapter_ext_id = ?
            """,
            updates,
        )

        conn.execute("DELETE FROM terminal_failures WHERE dataset_key = ?", (args.dataset_key,))
        if failure_payload:
            conn.executemany(
                """
                INSERT INTO terminal_failures(
                    dataset_key, chapter_ext_id, book_ext_id, failure_type, error_message,
                    raw_rel_path, source_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                failure_payload,
            )
            conn.executemany(
                """
                UPDATE chapters
                   SET download_status = ?,
                       download_error = ?,
                       updated_at = ?
                 WHERE dataset_key = ?
                   AND chapter_ext_id = ?
                """,
                chapter_failure_updates,
            )

        conn.execute(
            """
            UPDATE ingest_batches
               SET batch_status = ?,
                   updated_at = ?
             WHERE batch_id = ?
            """,
            ("download_sync_completed", now, batch_id),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"OK dataset_key={args.dataset_key}")
    print(f"OK batch_id={batch_id}")
    print(f"OK synced_files={len(updates)}")
    print(f"OK terminal_failures={len(failure_payload)}")


if __name__ == "__main__":
    main()
