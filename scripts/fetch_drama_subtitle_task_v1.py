from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
from urllib import error, request


def json_request(
    opener: request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:1000]}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a drama subtitle batch task through its dedicated API route.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    opener = request.build_opener(request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base_url = args.base_url.rstrip("/")
    login = json_request(
        opener,
        f"{base_url}/api/v1/auth/login",
        method="POST",
        payload={"username": args.username, "password": args.password},
    )
    detail = json_request(opener, f"{base_url}/api/v1/drama-subtitles/tasks/{args.task_id}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")

    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    items = detail.get("items") if isinstance(detail.get("items"), list) else []
    print(
        json.dumps(
            {
                "login_user": (login.get("user") or {}).get("username"),
                "task_id": args.task_id,
                "status": task.get("status"),
                "counts": task.get("counts"),
                "item_count": len(items),
                "out": str(out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
