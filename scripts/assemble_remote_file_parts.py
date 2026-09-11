from __future__ import annotations

import argparse
import os
import sys
import textwrap

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


def _run(client: paramiko.SSHClient, command: str, *, timeout: int = 3600) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise RuntimeError(f"remote command failed ({code}):\n{err or out}")
    return out


def assemble_parts(
    *,
    host: str,
    user: str,
    password: str,
    remote_dir: str,
    remote_name: str,
    expected_size: int | None,
) -> None:
    remote_dir = remote_dir.rstrip("/")
    parts_dir = f"{remote_dir}/{remote_name}.parts"
    output_path = f"{remote_dir}/{remote_name}"
    temp_path = f"{output_path}.assembling"

    remote_script = textwrap.dedent(
        f"""
        import glob
        import os
        import sys

        parts_dir = {parts_dir!r}
        remote_name = {remote_name!r}
        output_path = {output_path!r}
        temp_path = {temp_path!r}
        expected_size = {expected_size!r}

        part_pattern = os.path.join(parts_dir, remote_name + '.part*')
        parts = sorted(glob.glob(part_pattern))
        if not parts:
            raise SystemExit(f'no parts found: {{part_pattern}}')

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        if os.path.exists(temp_path):
            os.remove(temp_path)

        with open(temp_path, 'wb') as dst:
            for part_path in parts:
                with open(part_path, 'rb') as src:
                    while True:
                        buf = src.read(8 * 1024 * 1024)
                        if not buf:
                            break
                        dst.write(buf)

        os.replace(temp_path, output_path)
        final_size = os.path.getsize(output_path)
        if expected_size is not None and final_size != expected_size:
            raise SystemExit(
                f'assembled size mismatch: expected={{expected_size}} actual={{final_size}}'
            )

        print(f'parts={{len(parts)}}')
        print(f'output_path={{output_path}}')
        print(f'output_size={{final_size}}')
        """
    ).strip()

    command = "python3 - <<'PY'\n" + remote_script + "\nPY\n"
    client = _connect(host, user, password)
    try:
        out = _run(client, command)
    finally:
        client.close()
    print(out, end="" if out.endswith("\n") else "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", default="")
    parser.add_argument("--password-env", default="")
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--remote-name", required=True)
    parser.add_argument("--expected-size", type=int)
    args = parser.parse_args()

    password = args.password or os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit("Provide --password or --password-env with a populated environment variable.")
    assemble_parts(
        host=args.host,
        user=args.user,
        password=password,
        remote_dir=args.remote_dir,
        remote_name=args.remote_name,
        expected_size=args.expected_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
