from __future__ import annotations

import argparse
import csv
import json
import http.cookiejar
from pathlib import Path
from urllib import error, parse, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out-dir", default="docs")
    parser.add_argument("--page-limit", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_url = args.base_url.rstrip("/")
    cookie_jar = http.cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cookie_jar))

    login_payload = json.dumps({"username": args.username, "password": args.password}).encode("utf-8")
    login_request = request.Request(
        f"{base_url}/api/v1/auth/login",
        data=login_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(login_request, timeout=30) as response:
        login_body = response.read().decode("utf-8")
    login_json = json.loads(login_body)

    pages: list[dict[str, object]] = []
    all_items: list[dict[str, object]] = []
    offset = 0
    while True:
        detail_query = parse.urlencode({"item_limit": args.page_limit, "item_offset": offset})
        detail_request = request.Request(
            f"{base_url}/api/v1/tasks/{args.task_id}?{detail_query}",
            method="GET",
        )
        with opener.open(detail_request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        pages.append(payload)
        items = payload.get("items", [])
        all_items.extend(items)
        offset += len(items)
        if len(all_items) >= int(payload.get("item_total") or 0) or not items:
            break

    summary_request = request.Request(
        f"{base_url}/api/v1/tasks/{args.task_id}/exports/summary",
        method="GET",
    )
    with opener.open(summary_request, timeout=120) as response:
        summary_bytes = response.read()

    safe_task_id = args.task_id.replace("-", "")
    summary_path = out_dir / f"60_cloud_task_{safe_task_id}_summary.csv"
    summary_path.write_bytes(summary_bytes)

    raw_path = out_dir / f"60_cloud_task_{safe_task_id}_pages.json"
    raw_path.write_text(
        json.dumps({"pages": pages, "all_items": all_items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))

    print(
        json.dumps(
            {
                "login_user": login_json.get("user", {}).get("username"),
                "item_total": pages[0].get("item_total") if pages else 0,
                "fetched_items": len(all_items),
                "page_count": len(pages),
                "offsets": [page.get("item_offset") for page in pages],
                "limits": [page.get("item_limit") for page in pages],
                "task_status": pages[-1].get("task", {}).get("status") if pages else None,
                "counts": pages[-1].get("task", {}).get("counts") if pages else None,
                "result_stats": pages[-1].get("result_stats") if pages else None,
                "csv_row_count": len(csv_rows),
                "first_csv_columns": list(csv_rows[0].keys()) if csv_rows else [],
                "summary_path": str(summary_path),
                "raw_path": str(raw_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
