from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import uuid
from pathlib import Path
from urllib import error, request


def json_request(opener: request.OpenerDirector, url: str, payload: dict[str, str]) -> dict:
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:1000]}") from exc


def submit(opener: request.OpenerDirector, url: str, file_path: Path) -> dict:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    fields = {
        "top_k": "10",
        "window_limit": "200",
        "semantic_enabled": "true",
        "semantic_window_limit": "100",
        "translation_fallback": "true",
    }
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")
    mime = mimetypes.guess_type(file_path.name)[0] or "text/csv"
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
    )
    body.extend(file_path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = request.Request(
        url,
        data=bytes(body),
        headers={"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:1000]}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit a subtitle batch task without waiting for completion.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()

    opener = request.build_opener(request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base_url = args.base_url.rstrip("/")
    login = json_request(opener, f"{base_url}/api/v1/auth/login", {"username": args.username, "password": args.password})
    created = submit(opener, f"{base_url}/api/v1/drama-subtitles/tasks", Path(args.file).resolve())
    print(json.dumps({"login_user": (login.get("user") or {}).get("username"), **created}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
