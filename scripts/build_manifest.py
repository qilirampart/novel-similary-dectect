from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd


def pick_xlsx(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    files = [Path(p) for p in glob.glob("*.xlsx") if not Path(p).name.startswith("~$")]
    if not files:
        raise FileNotFoundError("No xlsx file found in current directory")
    if len(files) > 1:
        raise RuntimeError(f"Multiple xlsx files found: {[p.name for p in files]}")
    return files[0]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_url(signed_url: str) -> tuple[str, str, str, str, str]:
    parsed = urlparse(signed_url)
    object_key = parsed.path.lstrip("/")
    segments = [s for s in object_key.split("/") if s]
    if len(segments) < 2:
        raise ValueError(f"Bad object key: {object_key}")
    book_ext_id = segments[-2]
    file_name = segments[-1]
    chapter_id_from_url = file_name.removesuffix(".txt")
    expires_raw = parse_qs(parsed.query).get("Expires", [""])[0]
    expires_at = ""
    if expires_raw:
        expires_at = datetime.fromtimestamp(int(expires_raw)).isoformat(sep=" ", timespec="seconds")
    return parsed.netloc, object_key, book_ext_id, chapter_id_from_url, expires_at


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", help="Source xlsx path")
    parser.add_argument("--batch-id", help="Batch id, default batch_YYYYMMDD")
    parser.add_argument("--output-root", default="raw", help="Output root directory")
    args = parser.parse_args()

    source = pick_xlsx(args.xlsx)
    batch_id = args.batch_id or f"batch_{datetime.now():%Y%m%d}"
    batch_dir = Path(args.output_root) / batch_id
    manifest_dir = batch_dir / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(source, sheet_name=0)
    df = df.rename(columns={
        df.columns[0]: "book_name",
        df.columns[1]: "chapter_name",
        df.columns[2]: "chapter_id",
        df.columns[3]: "signed_url",
    })

    chapter_orders: dict[str, int] = defaultdict(int)
    rows: list[dict[str, object]] = []
    for row in df.itertuples(index=False):
        url_host, object_key, book_ext_id, chapter_id_from_url, expires_at = parse_url(str(row.signed_url))
        chapter_id = int(row.chapter_id)
        if str(chapter_id) != chapter_id_from_url:
            raise ValueError(f"chapter_id mismatch: {chapter_id} vs {chapter_id_from_url}")
        chapter_orders[book_ext_id] += 1
        rows.append(
            {
                "book_ext_id": book_ext_id,
                "book_name": str(row.book_name),
                "chapter_id": chapter_id,
                "chapter_name": str(row.chapter_name),
                "chapter_order": chapter_orders[book_ext_id],
                "url_host": url_host,
                "object_key": object_key,
                "signed_url": str(row.signed_url),
                "signed_expires_at": expires_at,
                "raw_rel_path": f"txt/{book_ext_id}/{chapter_id}.txt",
            }
        )

    out_csv = manifest_dir / "manifest.csv"
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    shutil.copy2(source, manifest_dir / source.name)
    sha = sha256_file(source)
    summary = manifest_dir / "manifest_summary.txt"
    unique_books = len({row["book_ext_id"] for row in rows})
    with summary.open("w", encoding="utf-8") as f:
        f.write(f"batch_id={batch_id}\n")
        f.write(f"source_file={source.name}\n")
        f.write(f"source_sha256={sha}\n")
        f.write(f"row_count={len(rows)}\n")
        f.write(f"unique_books={unique_books}\n")
        f.write(f"generated_at={datetime.now().isoformat(sep=' ', timespec='seconds')}\n")

    print(f"OK batch_dir={batch_dir}")
    print(f"OK manifest_csv={out_csv}")
    print(f"OK row_count={len(rows)} unique_books={unique_books}")


if __name__ == "__main__":
    main()
