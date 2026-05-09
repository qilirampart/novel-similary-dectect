from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--reason", default="HTTP Error 404: Not Found")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir)
    manifest = batch_dir / "manifest" / "manifest.csv"
    txt_root = batch_dir / "txt"
    out_csv = batch_dir / "logs" / "terminal_failures.csv"

    with manifest.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    expected = {row["raw_rel_path"].replace("/", "\\"): row for row in rows}
    actual = set()
    if txt_root.exists():
        for path in txt_root.rglob("*.txt"):
            actual.add(path.relative_to(batch_dir).as_posix().replace("/", "\\"))

    missing_keys = sorted(expected.keys() - actual)
    marked_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    result_rows = []
    for key in missing_keys:
        row = expected[key]
        result_rows.append(
            {
                "chapter_id": row["chapter_id"],
                "book_ext_id": row["book_ext_id"],
                "status": "terminal_missing",
                "error": args.reason,
                "raw_rel_path": row["raw_rel_path"],
                "marked_at": marked_at,
            }
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["chapter_id", "book_ext_id", "status", "error", "raw_rel_path", "marked_at"],
        )
        writer.writeheader()
        writer.writerows(result_rows)

    print(f"OK terminal_failures_csv={out_csv}")
    print(f"OK terminal_failures_count={len(result_rows)}")


if __name__ == "__main__":
    main()
