from __future__ import annotations

import argparse
import csv
import glob
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def pick_manifest(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    files = sorted(Path(p) for p in glob.glob(r"raw\batch_*\manifest\manifest.csv"))
    if not files:
        raise FileNotFoundError("No manifest.csv found under raw/batch_*/manifest/")
    return files[-1]


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
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity.sqlite3")
    parser.add_argument("--manifest", help="Path to manifest.csv")
    args = parser.parse_args()

    manifest = pick_manifest(args.manifest)
    manifest_dir = manifest.parent
    batch_id = manifest_dir.parent.name
    rows = read_csv(manifest)
    summary = read_summary(manifest_dir)
    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    db_path = Path(args.db)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ingest_batches(batch_id, source_file_name, source_file_sha256, source_row_count, created_at, note)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
                source_file_name=excluded.source_file_name,
                source_file_sha256=excluded.source_file_sha256,
                source_row_count=excluded.source_row_count,
                note=excluded.note
            """,
            (
                batch_id,
                summary.get("source_file", manifest.name),
                summary.get("source_sha256", ""),
                int(summary.get("row_count", len(rows))),
                now,
                f"manifest={manifest}",
            ),
        )

        conn.execute("DELETE FROM raw_manifest_rows WHERE batch_id = ?", (batch_id,))
        manifest_payload = []
        book_names: dict[str, str] = {}
        chapter_counts: defaultdict[str, int] = defaultdict(int)
        chapter_rows = []

        for idx, row in enumerate(rows, start=1):
            book_ext_id = row["book_ext_id"]
            book_names[book_ext_id] = row["book_name"]
            chapter_counts[book_ext_id] += 1
            manifest_payload.append(
                (
                    batch_id,
                    idx,
                    book_ext_id,
                    row["book_name"],
                    int(row["chapter_id"]),
                    row["chapter_name"],
                    int(row["chapter_order"]),
                    row["url_host"],
                    row["object_key"],
                    row["signed_url"],
                    row["signed_expires_at"],
                    row["raw_rel_path"],
                    now,
                )
            )
            chapter_rows.append(
                (
                    int(row["chapter_id"]),
                    book_ext_id,
                    row["chapter_name"],
                    int(row["chapter_order"]),
                    row["url_host"],
                    row["object_key"],
                    row["signed_url"],
                    row["signed_expires_at"],
                    now,
                    now,
                )
            )

        conn.executemany(
            """
            INSERT INTO raw_manifest_rows(
                batch_id, row_num, book_ext_id, book_name, chapter_id, chapter_name, chapter_order,
                url_host, object_key, signed_url, signed_expires_at, raw_rel_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            manifest_payload,
        )

        book_rows = [
            (book_ext_id, book_names[book_ext_id], chapter_counts[book_ext_id], now, now)
            for book_ext_id in sorted(book_names.keys())
        ]
        conn.executemany(
            """
            INSERT INTO books(book_ext_id, book_name, chapter_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(book_ext_id) DO UPDATE SET
                book_name=excluded.book_name,
                chapter_count=excluded.chapter_count,
                updated_at=excluded.updated_at
            """,
            book_rows,
        )

        conn.executemany(
            """
            INSERT INTO chapters(
                chapter_id, book_ext_id, chapter_name, chapter_order, url_host, object_key, signed_url,
                signed_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chapter_id) DO UPDATE SET
                book_ext_id=excluded.book_ext_id,
                chapter_name=excluded.chapter_name,
                chapter_order=excluded.chapter_order,
                url_host=excluded.url_host,
                object_key=excluded.object_key,
                signed_url=excluded.signed_url,
                signed_expires_at=excluded.signed_expires_at,
                updated_at=excluded.updated_at
            """,
            chapter_rows,
        )
        conn.commit()
    finally:
        conn.close()

    print(f"OK db={db_path}")
    print(f"OK batch_id={batch_id}")
    print(f"OK rows={len(rows)}")


if __name__ == "__main__":
    main()
