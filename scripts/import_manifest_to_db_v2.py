from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from v2_common import connect_db, infer_batch_id, now_ts, pick_manifest, read_csv, read_summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity.sqlite3")
    parser.add_argument("--manifest", help="Path to manifest.csv")
    parser.add_argument("--dataset-key", required=True)
    parser.add_argument("--dataset-label", default="")
    parser.add_argument("--source-scope", default="")
    args = parser.parse_args()

    manifest = pick_manifest(args.manifest)
    manifest_dir = manifest.parent
    batch_id = infer_batch_id(manifest)
    rows = read_csv(manifest)
    summary = read_summary(manifest_dir)
    now = now_ts()

    dataset_label = args.dataset_label or args.dataset_key
    source_file_name = summary.get("source_file", manifest.name)
    source_file_sha256 = summary.get("source_sha256", "")
    source_row_count = int(summary.get("row_count", len(rows)))

    db_path = Path(args.db)
    conn = connect_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO datasets(
                dataset_key, dataset_label, source_scope, source_file_name,
                source_file_sha256, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_key) DO UPDATE SET
                dataset_label=excluded.dataset_label,
                source_scope=COALESCE(excluded.source_scope, datasets.source_scope),
                source_file_name=excluded.source_file_name,
                source_file_sha256=excluded.source_file_sha256,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                args.dataset_key,
                dataset_label,
                args.source_scope or None,
                source_file_name,
                source_file_sha256,
                f"latest_manifest={manifest}",
                now,
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO ingest_batches(
                batch_id, dataset_key, source_file_name, source_file_sha256, source_row_count,
                manifest_path, batch_status, created_at, updated_at, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
                dataset_key=excluded.dataset_key,
                source_file_name=excluded.source_file_name,
                source_file_sha256=excluded.source_file_sha256,
                source_row_count=excluded.source_row_count,
                manifest_path=excluded.manifest_path,
                batch_status=excluded.batch_status,
                updated_at=excluded.updated_at,
                note=excluded.note
            """,
            (
                batch_id,
                args.dataset_key,
                source_file_name,
                source_file_sha256,
                source_row_count,
                str(manifest),
                "manifest_imported",
                now,
                now,
                f"dataset_key={args.dataset_key}",
            ),
        )

        conn.execute("DELETE FROM raw_manifest_rows WHERE batch_id = ?", (batch_id,))

        manifest_payload = []
        chapter_seed_rows = []
        book_names: dict[str, str] = {}
        chapter_counts: defaultdict[str, int] = defaultdict(int)

        for row_num, row in enumerate(rows, start=1):
            book_ext_id = row["book_ext_id"]
            chapter_ext_id = int(row["chapter_id"])
            chapter_order = int(row["chapter_order"])
            book_name = row["book_name"]
            chapter_name = row["chapter_name"]

            book_names[book_ext_id] = book_name
            chapter_counts[book_ext_id] += 1

            manifest_payload.append(
                (
                    batch_id,
                    row_num,
                    args.dataset_key,
                    book_ext_id,
                    book_name,
                    chapter_ext_id,
                    chapter_name,
                    chapter_order,
                    row["url_host"],
                    row["object_key"],
                    row["signed_url"],
                    row["signed_expires_at"],
                    row["raw_rel_path"],
                    now,
                )
            )
            chapter_seed_rows.append(
                (
                    book_ext_id,
                    chapter_ext_id,
                    chapter_name,
                    chapter_order,
                    row["url_host"],
                    row["object_key"],
                    row["signed_url"],
                    row["signed_expires_at"],
                )
            )

        conn.executemany(
            """
            INSERT INTO raw_manifest_rows(
                batch_id, row_num, dataset_key, book_ext_id, book_name, chapter_ext_id,
                chapter_name, chapter_order, url_host, object_key, signed_url,
                signed_expires_at, raw_rel_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            manifest_payload,
        )

        book_rows = [
            (
                args.dataset_key,
                book_ext_id,
                book_names[book_ext_id],
                chapter_counts[book_ext_id],
                now,
                now,
            )
            for book_ext_id in sorted(book_names)
        ]
        conn.executemany(
            """
            INSERT INTO books(
                dataset_key, book_ext_id, book_name, chapter_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_key, book_ext_id) DO UPDATE SET
                book_name=excluded.book_name,
                chapter_count=excluded.chapter_count,
                updated_at=excluded.updated_at
            """,
            book_rows,
        )

        book_uid_rows = conn.execute(
            """
            SELECT book_ext_id, book_uid
              FROM books
             WHERE dataset_key = ?
            """,
            (args.dataset_key,),
        ).fetchall()
        book_uid_map = {book_ext_id: book_uid for book_ext_id, book_uid in book_uid_rows}

        chapter_rows = [
            (
                args.dataset_key,
                book_uid_map[book_ext_id],
                chapter_ext_id,
                chapter_name,
                chapter_order,
                url_host,
                object_key,
                signed_url,
                signed_expires_at,
                now,
                now,
            )
            for (
                book_ext_id,
                chapter_ext_id,
                chapter_name,
                chapter_order,
                url_host,
                object_key,
                signed_url,
                signed_expires_at,
            ) in chapter_seed_rows
        ]

        conn.executemany(
            """
            INSERT INTO chapters(
                dataset_key, book_uid, chapter_ext_id, chapter_name, chapter_order,
                url_host, object_key, signed_url, signed_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_key, chapter_ext_id) DO UPDATE SET
                book_uid=excluded.book_uid,
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
    print(f"OK dataset_key={args.dataset_key}")
    print(f"OK batch_id={batch_id}")
    print(f"OK books={len(book_rows)}")
    print(f"OK chapters={len(chapter_rows)}")


if __name__ == "__main__":
    main()
