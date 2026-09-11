from __future__ import annotations

import argparse
import os
from pathlib import Path

import paramiko


def main() -> int:
    parser = argparse.ArgumentParser(description="Download the completed isolated cloud V2 subtitle probe result.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--remote-path", default="/opt/novel-similarity-service/shared/runtime/v2_subtitle_probe/result.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
    try:
        with client.open_sftp() as sftp:
            sftp.get(args.remote_path, str(output))
    finally:
        client.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
