from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import paramiko


def _ascii_value(resource_file: Path, line_number: int) -> str:
    lines = resource_file.read_bytes().splitlines()
    matches = re.findall(rb"[A-Za-z0-9._-]{4,}", lines[line_number])
    if not matches:
        raise ValueError(f"resource file line {line_number + 1} has no ASCII value")
    return matches[-1].decode("ascii")


def run(host: str, username: str, password: str, command: str, timeout: int = 600) -> str:
    last_error: Exception | None = None
    for attempt in range(2):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                username=username,
                password=password,
                timeout=20,
                banner_timeout=30,
                auth_timeout=30,
            )
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(15)
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            status = stdout.channel.recv_exit_status()
            output = stdout.read().decode("utf-8", errors="replace")
            error = stderr.read().decode("utf-8", errors="replace")
            if status != 0:
                return f"exit={status}\n{output}\n{error}".strip()
            return output.strip()
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(2)
        finally:
            client.close()
    return f"error={type(last_error).__name__}: {last_error}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only cloud storage audit.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-large-files", action="store_true")
    parser.add_argument("--only", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host = _ascii_value(args.resource_file, 2)
    username = _ascii_value(args.resource_file, 3)
    password = _ascii_value(args.resource_file, 4)
    commands = {
            "filesystem": "df -B1 /; df -ih /",
            "opt_top": "du -x -B1 --max-depth=1 /opt 2>/dev/null | sort -nr",
            "project_top": (
                "du -x -B1 --max-depth=2 /opt/novel-similarity-service "
                "2>/dev/null | sort -nr"
            ),
            "shared_top": (
                "du -x -B1 --max-depth=3 /opt/novel-similarity-service/shared "
                "2>/dev/null | sort -nr | head -n 100"
            ),
            "data_top": (
                "du -x -B1 --max-depth=2 /opt/novel-similarity-service/shared/data "
                "2>/dev/null | sort -nr | head -n 100"
            ),
            "backups_top": (
                "du -x -B1 --max-depth=2 /opt/novel-similarity-service/shared/backups "
                "2>/dev/null | sort -nr | head -n 100"
            ),
            "qdrant_snapshots_top": (
                "du -x -B1 --max-depth=2 "
                "/opt/novel-similarity-service/shared/qdrant_snapshots "
                "2>/dev/null | sort -nr | head -n 100"
            ),
            "qdrant_runtime_top": (
                "du -x -B1 --max-depth=3 /opt/novel-similarity-qdrant "
                "2>/dev/null | sort -nr | head -n 100"
            ),
            "qdrant_snapshot_files": (
                "find /opt/novel-similarity-qdrant/snapshots -maxdepth 3 -type f "
                "-printf '%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' 2>/dev/null "
                "| sort -nr | head -n 100"
            ),
            "qdrant_collections": (
                "curl -fsS http://127.0.0.1:6333/collections 2>/dev/null"
            ),
            "service_health": (
                "printf 'api_service='; systemctl is-active novel-similarity-api.service; "
                "printf 'qdrant_service='; systemctl is-active novel-similarity-qdrant.service; "
                "printf 'api_health='; curl -fsS --max-time 10 "
                "http://127.0.0.1:18101/api/v1/health/ready; "
                "printf '\nqdrant_health='; curl -fsS --max-time 10 http://127.0.0.1:6333/healthz; "
                "printf '\ncurrent='; readlink -f /opt/novel-similarity-service/current"
            ),
            "cleanup_candidates_absent": (
                "for p in "
                "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_20260902.sqlite3.parts "
                "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1_first7_20260904.sqlite3.parts "
                "/opt/novel-similarity-service/shared/data/drama_subtitle_similarity_v1.sqlite3.parts "
                "/opt/novel-similarity-service/shared/qdrant_snapshots/drama_subtitle_window_embeddings_qwen3_4b_2560_v1-20260903.snapshot.parts "
                "/opt/novel-similarity-service/shared/data/novel_similarity_v2_pipeline_smoketest.sqlite3 "
                "/opt/novel-similarity-service/releases/20260515-review-responsive-sync; "
                "do test ! -e \"$p\" || echo \"still_exists=$p\"; done"
            ),
            "large_candidate_files": (
                "find /opt/novel-similarity-service/shared/data "
                "/opt/novel-similarity-service/shared/backups "
                "/opt/novel-similarity-service/shared/qdrant_snapshots "
                "-xdev -type f -size +100M "
                "-printf '%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' 2>/dev/null "
                "| sort -nr | head -n 160"
            ),
            "releases": (
                "du -x -B1 --max-depth=1 /opt/novel-similarity-service/releases "
                "2>/dev/null | sort -nr"
            ),
            "qdrant_service": (
                "systemctl show novel-similarity-qdrant.service "
                "--property=ActiveState,SubState,MainPID,ExecStart,WorkingDirectory,FragmentPath "
                "--no-pager"
            ),
            "api_service": (
                "systemctl show novel-similarity-api.service "
                "--property=ActiveState,SubState,MainPID,ExecStart,WorkingDirectory,FragmentPath "
                "--no-pager"
            ),
            "current_release": (
                "readlink -f /opt/novel-similarity-service/current; "
                "find /opt/novel-similarity-service -maxdepth 3 -type l "
                "-printf '%p -> %l\n' 2>/dev/null | sort"
            ),
            "qdrant_process": (
                "pid=$(systemctl show novel-similarity-qdrant.service --property=MainPID --value); "
                "test -n \"$pid\" && tr '\0' ' ' </proc/$pid/cmdline"
            ),
            "service_storage_references": (
                "systemctl cat novel-similarity-qdrant.service novel-similarity-api.service "
                "2>/dev/null | grep -E '^(WorkingDirectory|ExecStart|EnvironmentFile)|storage|snapshot|sqlite' "
                "| sed -E 's/(KEY|SECRET|TOKEN|PASSWORD)=[^ ]+/\\1=<redacted>/g'; "
                "pid=$(systemctl show novel-similarity-api.service --property=MainPID --value); "
                "find /proc/$pid/fd -maxdepth 1 -type l -printf '%l\n' 2>/dev/null "
                "| grep -E '/opt/novel-similarity|sqlite|qdrant' | sort -u"
            ),
            "configured_database_paths": (
                "grep -E '^(NOVEL_SIMILARITY_DB|DRAMA_SUBTITLE_SIMILARITY_DB|"
                "QDRANT_COLLECTION|DRAMA_SUBTITLE_QDRANT_COLLECTION)=' "
                "/opt/novel-similarity-service/shared/novel-similarity.env "
                "2>/dev/null"
            ),
            "journals_and_tmp": (
                "journalctl --disk-usage; "
                "du -x -B1 --max-depth=2 /var/tmp /tmp "
                "/opt/novel-similarity-service/shared/tmp "
                "/opt/novel-similarity-service/shared/logs 2>/dev/null | sort -nr"
            ),
            "deleted_open_files": (
                "lsof +L1 2>/dev/null | awk 'NR==1 || $7 ~ /^[0-9]+$/' | head -n 100"
            ),
    }
    if args.include_large_files:
        commands["project_large_files"] = (
            "find /opt/novel-similarity-service -xdev -type f -size +100M "
            "-printf '%s\t%TY-%Tm-%Td %TH:%TM\t%p\n' 2>/dev/null "
            "| sort -nr | head -n 120"
        )
    if args.only:
        unknown = sorted(set(args.only) - commands.keys())
        if unknown:
            raise ValueError(f"unknown sections: {', '.join(unknown)}")
        commands = {name: commands[name] for name in args.only}

    result: dict[str, str] = {}
    for name, command in commands.items():
        result[name] = run(host, username, password, command)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
