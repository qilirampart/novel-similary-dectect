from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from upload_large_file_parts import upload_parts


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload SFTP parts with reconnect retries per part.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--local-path", required=True)
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--remote-name", required=True)
    parser.add_argument("--part-size-mib", type=int, default=128)
    parser.add_argument("--io-chunk-mib", type=int, default=8)
    parser.add_argument("--part-start", type=int, default=0)
    parser.add_argument("--part-end", type=int, required=True)
    parser.add_argument("--retries", type=int, default=6)
    args = parser.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")

    for part_index in range(max(args.part_start, 0), max(args.part_end, 0)):
        for attempt in range(1, max(args.retries, 1) + 1):
            try:
                upload_parts(
                    host=args.host,
                    user=args.user,
                    password=password,
                    local_path=Path(args.local_path),
                    remote_dir=args.remote_dir,
                    remote_name=args.remote_name,
                    part_size_mib=args.part_size_mib,
                    io_chunk_mib=args.io_chunk_mib,
                    part_start=part_index,
                    part_end=part_index + 1,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= max(args.retries, 1):
                    raise
                delay = min(30, 2**attempt)
                print(f"retry part={part_index:05d} attempt={attempt} delay_seconds={delay} error={exc}", flush=True)
                time.sleep(delay)
    print("OK resumable upload completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
