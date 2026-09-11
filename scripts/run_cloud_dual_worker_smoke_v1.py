from __future__ import annotations

import argparse
import http.cookiejar
import json
import mimetypes
import sys
import time
import uuid
from pathlib import Path
from urllib import parse, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-seconds", type=float, default=0.8)
    parser.add_argument("--delete-after", action="store_true")
    return parser.parse_args()


def build_opener() -> request.OpenerDirector:
    cookie_jar = http.cookiejar.CookieJar()
    return request.build_opener(request.HTTPCookieProcessor(cookie_jar))


def json_request(
    opener: request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def multipart_request(
    opener: request.OpenerDirector,
    url: str,
    *,
    file_path: Path,
    detection_mode: str,
    threshold: str,
    timeout: float = 120.0,
) -> dict[str, object]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    file_bytes = file_path.read_bytes()

    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    add_field("detection_mode", detection_mode)
    add_field("candidate_display_score_threshold", threshold)

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_task_detail(opener: request.OpenerDirector, base_url: str, task_id: str) -> dict[str, object]:
    query = parse.urlencode({"item_limit": 1, "item_offset": 0})
    return json_request(opener, f"{base_url}/api/v1/tasks/{task_id}?{query}", timeout=60.0)


def get_task_list(opener: request.OpenerDirector, base_url: str, limit: int = 100) -> list[dict[str, object]]:
    query = parse.urlencode({"limit": limit, "offset": 0})
    payload = json_request(opener, f"{base_url}/api/v1/tasks?{query}", timeout=60.0)
    return list(payload.get("items", []))


def delete_task(opener: request.OpenerDirector, base_url: str, task_id: str) -> dict[str, object]:
    return json_request(opener, f"{base_url}/api/v1/tasks/{task_id}", method="DELETE", timeout=60.0)


def get_with_retry(fetch_fn, *args, retries: int = 3, sleep_seconds: float = 1.0, **kwargs):
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            return fetch_fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 >= retries:
                raise
            time.sleep(sleep_seconds)
    raise RuntimeError(f"unexpected retry state: {last_error}")


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    file_path = Path(args.file)
    opener = build_opener()

    login_payload = json_request(
        opener,
        f"{base_url}/api/v1/auth/login",
        method="POST",
        payload={"username": args.username, "password": args.password},
        timeout=30.0,
    )

    task_payloads = []
    for _ in range(2):
        task_payloads.append(
            multipart_request(
                opener,
                f"{base_url}/api/v1/tasks",
                file_path=file_path,
                detection_mode="rewrite",
                threshold="0.01",
                timeout=120.0,
            )
        )

    task_ids = [str(item["task_id"]) for item in task_payloads]
    print(json.dumps({"created_task_ids": task_ids}, ensure_ascii=False), file=sys.stderr)
    deadline = time.time() + args.timeout_seconds
    poll_samples: list[dict[str, object]] = []
    overlap_observed = False
    overlap_workers: list[str] = []
    final_details: dict[str, dict[str, object]] = {}
    poll_errors: list[str] = []

    while time.time() < deadline:
        snapshot: dict[str, object] = {
            "ts": round(time.time(), 3),
            "tasks": [],
        }
        running_workers: list[str] = []
        completed_count = 0

        try:
            all_tasks = get_with_retry(get_task_list, opener, base_url, retries=2, sleep_seconds=1.0)
        except Exception as exc:  # noqa: BLE001
            poll_errors.append(f"task-list poll failed: {type(exc).__name__}: {exc}")
            time.sleep(args.poll_seconds)
            continue

        indexed = {str(item["task_id"]): item for item in all_tasks}

        for task_id in task_ids:
            task = indexed.get(task_id)
            if task is None:
                snapshot["tasks"].append(
                    {
                        "task_id": task_id,
                        "status": "missing_from_list",
                        "worker_name": "",
                        "accepted": None,
                        "completed": None,
                        "failed": None,
                        "status_message": "",
                    }
                )
                continue
            counts = task["counts"]
            task_info = {
                "task_id": task_id,
                "status": task["status"],
                "worker_name": task.get("worker_name") or "",
                "accepted": counts["accepted"],
                "completed": counts["completed"],
                "failed": counts["failed"],
                "status_message": task.get("status_message") or "",
            }
            snapshot["tasks"].append(task_info)
            if task["status"] == "running":
                worker_name = str(task.get("worker_name") or "")
                if worker_name:
                    running_workers.append(worker_name)
            if task["status"] == "completed":
                completed_count += 1

        poll_samples.append(snapshot)

        unique_running_workers = sorted(set(running_workers))
        if len(unique_running_workers) >= 2:
            overlap_observed = True
            overlap_workers = unique_running_workers

        if completed_count == len(task_ids):
            break
        time.sleep(args.poll_seconds)

    for task_id in task_ids:
        try:
            final_details[task_id] = get_with_retry(
                get_task_detail,
                opener,
                base_url,
                task_id,
                retries=4,
                sleep_seconds=2.0,
            )
        except Exception as exc:  # noqa: BLE001
            poll_errors.append(f"final detail failed for {task_id}: {type(exc).__name__}: {exc}")

    delete_results: dict[str, dict[str, object]] = {}
    if args.delete_after:
        for task_id in task_ids:
            try:
                delete_results[task_id] = delete_task(opener, base_url, task_id)
            except Exception as exc:  # noqa: BLE001
                delete_results[task_id] = {"error": f"{type(exc).__name__}: {exc}"}

    condensed_samples = []
    for sample in poll_samples[:12]:
        condensed_samples.append(sample)
    if len(poll_samples) > 12:
        condensed_samples.append({"omitted_samples": len(poll_samples) - 12})
        condensed_samples.extend(poll_samples[-4:])

    result = {
        "login_user": login_payload.get("user", {}).get("username"),
        "file": str(file_path),
        "task_ids": task_ids,
        "overlap_observed": overlap_observed,
        "overlap_workers": overlap_workers,
        "poll_count": len(poll_samples),
        "poll_errors": poll_errors,
        "samples": condensed_samples,
        "final": {
            task_id: {
                "status": detail.get("task", {}).get("status"),
                "worker_name": detail.get("task", {}).get("worker_name") or "",
                "accepted": detail.get("task", {}).get("counts", {}).get("accepted"),
                "completed": detail.get("task", {}).get("counts", {}).get("completed"),
                "failed": detail.get("task", {}).get("counts", {}).get("failed"),
                "started_at": detail.get("task", {}).get("started_at"),
                "finished_at": detail.get("task", {}).get("finished_at"),
                "semantic_fallback_count": detail.get("result_stats", {}).get("semantic_fallback_count"),
                "item_total": detail.get("item_total"),
            }
            for task_id, detail in final_details.items()
        },
        "delete_results": delete_results,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
