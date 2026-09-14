from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


TERMINAL_STATUSES = {"completed", "partial_failed", "failed", "cancelled"}
CHANNEL_ID_PATTERN = re.compile(r"(?:channel/)?(UC[A-Za-z0-9_-]{20,})")


def extract_channel_id(value: str) -> str:
    match = CHANNEL_ID_PATTERN.search(str(value or ""))
    if match is None:
        raise ValueError("Channel URL does not contain a YouTube channel ID")
    return match.group(1)


def require_json(response: requests.Response) -> dict[str, Any]:
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("API response is not a JSON object")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a 10-item real cloud cover smoke test.")
    parser.add_argument("--base-url", default="http://novel-similarity-dev.dzkjm.cn")
    parser.add_argument("--username", default="operator1")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--channel-id", default="")
    parser.add_argument("--max-items", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/cover_cloud_smoke_20260914/real10_result.json"),
    )
    parser.add_argument(
        "--export",
        type=Path,
        default=Path("runtime/cover_cloud_smoke_20260914/new_findings.xlsx"),
    )
    return parser.parse_args()


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    overall_started = time.perf_counter()
    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"Missing password in environment variable: {args.password_env}")
    if not args.input.is_file() or args.input.suffix.lower() != ".xlsx":
        raise SystemExit("Input must be an existing .xlsx file")
    if not 1 <= args.max_items <= 100:
        raise SystemExit("max-items must be between 1 and 100 for smoke testing")

    base_url = args.base_url.rstrip("/")
    session = requests.Session()
    report: dict[str, Any] = {
        "base_url": base_url,
        "input": str(args.input),
        "max_items": args.max_items,
        "started_at_epoch": time.time(),
        "stages": {},
    }

    started = time.perf_counter()
    login = require_json(
        session.post(
            f"{base_url}/api/v1/auth/login",
            json={"username": args.username, "password": password},
            timeout=30,
        )
    )
    report["stages"]["login"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "username": login.get("username"),
    }
    save_report(args.report, report)

    started = time.perf_counter()
    with args.input.open("rb") as stream:
        preview = require_json(
            session.post(
                f"{base_url}/api/v1/cover-monitor/imports/preview",
                files={"file": (args.input.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"import_kind": "channels"},
                timeout=300,
            )
        )
    report["stages"]["preview"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "import_id": preview.get("import_id"),
        "status": preview.get("status"),
        "stats": preview.get("stats"),
        "conflict_sample_count": len(preview.get("conflict_samples") or []),
    }
    save_report(args.report, report)

    started = time.perf_counter()
    confirmed = require_json(
        session.post(
            f"{base_url}/api/v1/cover-monitor/imports/{preview['import_id']}/confirm",
            timeout=300,
        )
    )
    report["stages"]["confirm"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "status": confirmed.get("status"),
        "stats": confirmed.get("stats"),
    }
    save_report(args.report, report)

    channel_id = args.channel_id.strip() or extract_channel_id(
        next(iter(preview.get("mapping", {}).values()), "")
    )
    channel_response = require_json(
        session.get(
            f"{base_url}/api/v1/cover-monitor/channels",
            params={"keyword": channel_id, "limit": 10, "offset": 0},
            timeout=30,
        )
    )
    matching_channels = [
        item for item in channel_response.get("items", []) if item.get("channel_id") == channel_id
    ]
    if len(matching_channels) != 1:
        raise RuntimeError(f"Expected one imported channel for {channel_id}, found {len(matching_channels)}")
    channel = matching_channels[0]

    started = time.perf_counter()
    run = require_json(
        session.post(
            f"{base_url}/api/v1/cover-monitor/runs",
            json={
                "intensity": "standard",
                "channel_pks": [channel["channel_pk"]],
                "include_shorts": False,
                "force_refresh": True,
                "max_items_per_scope": args.max_items,
            },
            timeout=30,
        )
    )
    run_id = str(run["run_id"])
    report["stages"]["run"] = {
        "run_id": run_id,
        "channel_id": channel_id,
        "channel_pk": channel["channel_pk"],
        "status": run.get("status"),
    }
    save_report(args.report, report)

    deadline = time.monotonic() + max(args.timeout_seconds, 1)
    polls = 0
    while str(run.get("status")) not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Cover run {run_id} did not finish within {args.timeout_seconds}s")
        time.sleep(5)
        polls += 1
        run = require_json(
            session.get(f"{base_url}/api/v1/cover-monitor/runs/{run_id}", timeout=30)
        )
        report["stages"]["run"].update(
            {
                "status": run.get("status"),
                "completed_item_count": run.get("completed_item_count"),
                "failed_item_count": run.get("failed_item_count"),
                "total_item_count": run.get("total_item_count"),
                "polls": polls,
            }
        )
        save_report(args.report, report)

    report["stages"]["run"]["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    detail = require_json(
        session.get(
            f"{base_url}/api/v1/cover-monitor/runs/{run_id}/detail",
            params={"item_limit": 100, "item_offset": 0},
            timeout=30,
        )
    )
    report["result"] = {
        "run": detail.get("run"),
        "channels": detail.get("channels"),
        "items": detail.get("items"),
    }

    export_response = session.get(
        f"{base_url}/api/v1/cover-monitor/runs/{run_id}/exports/new-findings",
        timeout=120,
    )
    if not export_response.ok:
        raise RuntimeError(f"Export HTTP {export_response.status_code}: {export_response.text[:500]}")
    args.export.parent.mkdir(parents=True, exist_ok=True)
    args.export.write_bytes(export_response.content)
    report["export"] = {
        "path": str(args.export),
        "bytes": len(export_response.content),
        "content_type": export_response.headers.get("content-type"),
    }
    report["finished_at_epoch"] = time.time()
    report["total_elapsed_seconds"] = round(time.perf_counter() - overall_started, 3)
    save_report(args.report, report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
