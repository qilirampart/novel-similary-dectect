from __future__ import annotations

import argparse
import csv
import http.cookiejar
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request


TERMINAL_STATUSES = {"completed", "partial_failed", "failed", "cancelled", "paused"}


def multipart_request(opener: request.OpenerDirector, url: str, file_path: Path) -> dict[str, Any]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    body = bytearray()
    fields = {
        "top_k": "10",
        "window_limit": "200",
        "semantic_enabled": "true",
        "semantic_window_limit": "100",
        "translation_fallback": "true",
    }
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


def json_request(opener: request.OpenerDirector, url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:1000]}") from exc


def text(value: Any) -> str:
    return "" if value is None else str(value)


def payload_from_item(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("result_payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(text(raw) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def first_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return {}


def compact(value: Any) -> str:
    if value in (None, ""):
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, source_rows: list[dict[str, str]], items: list[dict[str, Any]]) -> None:
    by_order = {int(item.get("item_order")): item for item in items if text(item.get("item_order")).isdigit()}
    headers = [
        "序号", "视频 ID", "来源频道", "来源剧名", "基准原版剧ID", "基准原版剧名称",
        "最终状态", "命中状态", "是否确认命中", "判定原因", "用户说明", "耗时(秒)",
        "语义状态", "第一候选剧名", "第一候选Book ID", "第一候选集数", "第一候选语言",
        "第一候选语义分数", "第一候选精排分数", "第一候选证据文本", "候选列表", "错误信息", "原始字幕",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for order, source in enumerate(source_rows, start=1):
            item = by_order.get(order, {})
            payload = payload_from_item(item)
            decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
            candidate = first_candidate(payload)
            evidence = candidate.get("evidence")
            if isinstance(evidence, dict):
                evidence = evidence.get("window_text") or evidence.get("text") or ""
            writer.writerow({
                "序号": order,
                "视频 ID": source.get("source_video_id", ""),
                "来源频道": source.get("source_channel", ""),
                "来源剧名": source.get("short_drama", ""),
                "基准原版剧ID": source.get("baseline_original_id", ""),
                "基准原版剧名称": source.get("baseline_original_name", ""),
                "最终状态": item.get("status", ""),
                "命中状态": payload.get("hit_status", decision.get("hit_status", "")),
                "是否确认命中": payload.get("is_confirmed_match", decision.get("is_confirmed_match", "")),
                "判定原因": decision.get("reason", ""),
                "用户说明": decision.get("user_message", ""),
                "耗时(秒)": item.get("duration_seconds", ""),
                "语义状态": item.get("semantic_status", ""),
                "第一候选剧名": candidate.get("book_name") or candidate.get("matched_book_name", ""),
                "第一候选Book ID": candidate.get("book_id") or candidate.get("matched_book_id", ""),
                "第一候选集数": candidate.get("episode_order") or candidate.get("matched_episode_order", ""),
                "第一候选语言": candidate.get("language_code", ""),
                "第一候选语义分数": candidate.get("semantic_score", ""),
                "第一候选精排分数": candidate.get("fine_score", ""),
                "第一候选证据文本": evidence or candidate.get("candidate_text", ""),
                "候选列表": compact(payload.get("content_candidate_options") or payload.get("candidates")),
                "错误信息": item.get("error_message", ""),
                "原始字幕": source.get("query_text", ""),
            })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--out-dir", default="docs/42_coverage_rerun_20260902")
    parser.add_argument("--expected-rows", type=int, default=42)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    input_path = Path(args.file).resolve()
    source_rows = load_rows(input_path)
    if len(source_rows) != args.expected_rows:
        raise RuntimeError(f"expected {args.expected_rows} input rows, got {len(source_rows)}")
    opener = request.build_opener(request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login = json_request(opener, f"{base_url}/api/v1/auth/login", method="POST", payload={"username": args.username, "password": args.password})
    created = multipart_request(opener, f"{base_url}/api/v1/drama-subtitles/tasks", input_path)
    task_id = text(created.get("task_id"))
    if not task_id:
        raise RuntimeError(f"task creation returned no task_id: {created}")
    print(json.dumps({"login_user": (login.get("user") or {}).get("username"), "task_id": task_id}, ensure_ascii=False), flush=True)
    started = time.monotonic()
    poll_log: list[dict[str, Any]] = []
    detail: dict[str, Any] = {}
    while True:
        detail = json_request(opener, f"{base_url}/api/v1/drama-subtitles/tasks/{parse.quote(task_id)}")
        task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
        snapshot = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "status": task.get("status"),
            "counts": task.get("counts"),
            "item_count": len(detail.get("items") or []),
        }
        poll_log.append(snapshot)
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        if text(task.get("status")) in TERMINAL_STATUSES:
            break
        if time.monotonic() - started > args.timeout_seconds:
            raise TimeoutError(f"task timed out: {task_id}")
        time.sleep(args.poll_seconds)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"task_{task_id}_raw.json").write_text(json.dumps({"task": detail.get("task"), "items": detail.get("items", []), "poll_log": poll_log}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(out_dir / "42条字幕库补充后重跑_逐条结果.csv", source_rows, detail.get("items") or [])
    summary = {
        "task_id": task_id,
        "status": (detail.get("task") or {}).get("status"),
        "counts": (detail.get("task") or {}).get("counts"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output_dir": str(out_dir),
    }
    (out_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
