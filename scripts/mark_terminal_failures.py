from __future__ import annotations

import argparse
import csv
import glob
from datetime import datetime
from pathlib import Path


def pick_results(batch_dir: Path) -> Path:
    files = sorted(Path(p) for p in glob.glob(str(batch_dir / "logs" / "download_results_*.csv")))
    if not files:
        raise FileNotFoundError(f"No download_results csv found under {batch_dir / 'logs'}")
    return files[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--results-csv", help="Specific download_results csv path")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir)
    results_csv = Path(args.results_csv) if args.results_csv else pick_results(batch_dir)
    out_csv = batch_dir / "logs" / "terminal_failures.csv"

    rows = []
    with results_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            error = row.get("error", "")
            status = row.get("status", "")
            if status == "failed" and "HTTP Error 404" in error:
                rows.append(
                    {
                        "chapter_id": row["chapter_id"],
                        "book_ext_id": row["book_ext_id"],
                        "status": "terminal_404",
                        "error": error,
                        "source_results_csv": str(results_csv),
                        "marked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                    }
                )

    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "chapter_id",
                "book_ext_id",
                "status",
                "error",
                "source_results_csv",
                "marked_at",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"OK results_csv={results_csv}")
    print(f"OK terminal_failures_csv={out_csv}")
    print(f"OK terminal_failures_count={len(rows)}")


if __name__ == "__main__":
    main()
