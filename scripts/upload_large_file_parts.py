from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import time

import paramiko


def _connect(host: str, user: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        compress=True,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def _ensure_remote_dir(client: paramiko.SSHClient, remote_dir: str) -> None:
    stdin, stdout, stderr = client.exec_command(f"mkdir -p {remote_dir}")
    stdout.read()
    stderr.read()


def _remote_file_size(client: paramiko.SSHClient, remote_path: str) -> int | None:
    sftp = client.open_sftp()
    try:
        return sftp.stat(remote_path).st_size
    except FileNotFoundError:
        return None
    finally:
        sftp.close()


def upload_parts(
    *,
    host: str,
    user: str,
    password: str,
    local_path: Path,
    remote_dir: str,
    remote_name: str,
    part_size_mib: int,
    io_chunk_mib: int,
    part_start: int = 0,
    part_end: int | None = None,
) -> None:
    file_size = local_path.stat().st_size
    part_size = part_size_mib * 1024 * 1024
    io_chunk = io_chunk_mib * 1024 * 1024
    part_count = int(math.ceil(file_size / part_size))
    remote_parts_dir = f"{remote_dir.rstrip('/')}/{remote_name}.parts"

    print(f"local_path={local_path}")
    print(f"remote_parts_dir={remote_parts_dir}")
    print(f"file_size={file_size}")
    print(f"part_size={part_size}")
    print(f"part_count={part_count}")

    effective_start = max(0, int(part_start))
    effective_end = part_count if part_end is None else min(part_count, int(part_end))
    if effective_start >= effective_end:
        raise ValueError(f"invalid part range: {effective_start}:{effective_end}")
    for part_index in range(effective_start, effective_end):
        offset = part_index * part_size
        expected_size = min(part_size, file_size - offset)
        remote_part_path = f"{remote_parts_dir}/{remote_name}.part{part_index:05d}"

        client = _connect(host, user, password)
        try:
            _ensure_remote_dir(client, remote_parts_dir)
            existing_size = _remote_file_size(client, remote_part_path)
            if existing_size == expected_size:
                print(f"skip part={part_index:05d} size={existing_size}")
                continue

            if existing_size not in (None, expected_size):
                stdin, stdout, stderr = client.exec_command(f"rm -f {remote_part_path}")
                stdout.read()
                stderr.read()

            print(
                f"upload part={part_index:05d} offset={offset} "
                f"size={expected_size} remote={remote_part_path}"
            )
            started = time.time()
            sent = 0
            with local_path.open("rb") as src:
                src.seek(offset)
                sftp = client.open_sftp()
                try:
                    with sftp.open(remote_part_path, "wb") as dst:
                        # Pipeline SFTP writes to avoid waiting for every packet round trip.
                        dst.set_pipelined(True)
                        remaining = expected_size
                        while remaining > 0:
                            buf = src.read(min(io_chunk, remaining))
                            if not buf:
                                break
                            dst.write(buf)
                            remaining -= len(buf)
                            sent += len(buf)
                            if sent == expected_size or sent % (512 * 1024 * 1024) < len(buf):
                                elapsed = max(time.time() - started, 0.001)
                                rate = sent / elapsed / (1024 * 1024)
                                print(
                                    f"progress part={part_index:05d} "
                                    f"{sent}/{expected_size} rate_mib_s={rate:.2f}"
                                )
                finally:
                    sftp.close()

            final_size = _remote_file_size(client, remote_part_path)
            if final_size != expected_size:
                raise RuntimeError(
                    f"part size mismatch part={part_index:05d} expected={expected_size} got={final_size}"
                )
        finally:
            client.close()

    print("OK all parts uploaded")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", default="")
    parser.add_argument("--password-env", default="")
    parser.add_argument("--local-path", required=True)
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--remote-name", required=True)
    parser.add_argument("--part-size-mib", type=int, default=512)
    parser.add_argument("--io-chunk-mib", type=int, default=8)
    parser.add_argument("--part-start", type=int, default=0)
    parser.add_argument("--part-end", type=int)
    args = parser.parse_args()

    password = args.password or os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit("Provide --password or --password-env with a populated environment variable.")
    upload_parts(
        host=args.host,
        user=args.user,
        password=password,
        local_path=Path(args.local_path),
        remote_dir=args.remote_dir,
        remote_name=args.remote_name,
        part_size_mib=args.part_size_mib,
        io_chunk_mib=args.io_chunk_mib,
        part_start=args.part_start,
        part_end=args.part_end,
    )


if __name__ == "__main__":
    main()
