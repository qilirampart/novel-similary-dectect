from __future__ import annotations

import argparse
import csv
import glob
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

THREAD_LOCAL = threading.local()
CHUNK_SIZE = 1024 * 256


def pick_manifest(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    files = sorted(Path(p) for p in glob.glob(r"raw\batch_*\manifest\manifest.csv"))
    if not files:
        raise FileNotFoundError("No manifest.csv found under raw/batch_*/manifest/")
    return files[-1]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def resolve_dest(row: dict[str, str], txt_root: Path) -> Path:
    rel_path = row["raw_rel_path"]
    return txt_root / Path(rel_path).relative_to("txt")


def acquire_lock(lock_path: Path) -> int:
    ensure_parent(lock_path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(lock_path), flags)
    payload = [
        f"pid={os.getpid()}",
        f"started_at={datetime.now().isoformat(sep=' ', timespec='seconds')}",
    ]
    os.write(fd, ("\n".join(payload) + "\n").encode("utf-8"))
    return fd


def release_lock(fd: int, lock_path: Path) -> None:
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "novel-similarity-downloader/2.0",
            "Connection": "keep-alive",
        }
    )
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0, pool_block=True)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_session() -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = build_session()
        THREAD_LOCAL.session = session
    return session


def download_one(
    row: dict[str, str],
    txt_root: Path,
    timeout: int,
    overwrite: bool,
    retries: int,
) -> dict[str, str]:
    chapter_id = row["chapter_id"]
    book_ext_id = row["book_ext_id"]
    signed_url = row["signed_url"]
    dest = resolve_dest(row, txt_root)
    ensure_parent(dest)
    if dest.exists() and not overwrite:
        return {
            "chapter_id": chapter_id,
            "book_ext_id": book_ext_id,
            "status": "skipped_exists",
            "file_path": str(dest),
            "error": "",
            "downloaded_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }

    last_error = ""
    session = get_session()
    for attempt in range(retries + 1):
        tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.{threading.get_ident()}.part")
        try:
            with session.get(signed_url, timeout=timeout, stream=True) as resp, tmp.open("wb") as f:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, dest)
            return {
                "chapter_id": chapter_id,
                "book_ext_id": book_ext_id,
                "status": "downloaded",
                "file_path": str(dest),
                "error": "",
                "downloaded_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            }
        except (requests.RequestException, TimeoutError, OSError) as e:
            last_error = str(e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if dest.exists() and not overwrite:
                return {
                    "chapter_id": chapter_id,
                    "book_ext_id": book_ext_id,
                    "status": "skipped_exists",
                    "file_path": str(dest),
                    "error": "",
                    "downloaded_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                }
            if attempt < retries:
                time.sleep(min(2 ** attempt, 5))

    return {
        "chapter_id": chapter_id,
        "book_ext_id": book_ext_id,
        "status": "failed",
        "file_path": str(dest),
        "error": last_error,
        "downloaded_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", help="manifest.csv path")
    parser.add_argument("--limit", type=int, default=0, help="Only download first N rows")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-inflight", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    manifest = pick_manifest(args.manifest)
    batch_dir = manifest.parent.parent
    txt_root = batch_dir / "txt"
    logs_dir = batch_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = logs_dir / "download.lock"
    rows = read_rows(manifest)
    if not args.overwrite:
        rows = [row for row in rows if not resolve_dest(row, txt_root).exists()]
    if args.limit > 0:
        rows = rows[: args.limit]

    progress_lock = threading.Lock()
    done = 0
    total = len(rows)
    max_inflight = args.max_inflight if args.max_inflight > 0 else max(args.workers * 8, args.workers)

    if total == 0:
        print(f"OK manifest={manifest}")
        print("OK nothing_to_download=1")
        return

    def task(row: dict[str, str]) -> dict[str, str]:
        nonlocal done
        result = download_one(
            row,
            txt_root=txt_root,
            timeout=args.timeout,
            overwrite=args.overwrite,
            retries=args.retries,
        )
        with progress_lock:
            done += 1
            if done % 100 == 0 or done == total:
                print(f"progress {done}/{total}")
        return result

    out_csv = logs_dir / f"download_results_{datetime.now():%Y%m%d_%H%M%S}.csv"
    try:
        lock_fd = acquire_lock(lock_path)
    except FileExistsError:
        raise SystemExit(f"Downloader lock exists: {lock_path}")
    downloaded = 0
    skipped = 0
    failed = 0
    try:
        with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["chapter_id", "book_ext_id", "status", "file_path", "error", "downloaded_at"],
            )
            writer.writeheader()

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                row_iter = iter(rows)
                futures: set = set()

                while len(futures) < min(max_inflight, total):
                    try:
                        row = next(row_iter)
                    except StopIteration:
                        break
                    futures.add(executor.submit(task, row))

                while futures:
                    done_futures, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done_futures:
                        result = future.result()
                        writer.writerow(result)
                        if result["status"] == "downloaded":
                            downloaded += 1
                        elif result["status"] == "skipped_exists":
                            skipped += 1
                        else:
                            failed += 1
                        if (downloaded + skipped + failed) % 100 == 0 or (downloaded + skipped + failed) == total:
                            f.flush()

                        try:
                            row = next(row_iter)
                        except StopIteration:
                            continue
                        futures.add(executor.submit(task, row))
    finally:
        release_lock(lock_fd, lock_path)

    print(f"OK manifest={manifest}")
    print(f"OK log={out_csv}")
    print(f"OK downloaded={downloaded} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
