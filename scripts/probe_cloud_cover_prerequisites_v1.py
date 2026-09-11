from __future__ import annotations

import argparse
import base64
import json
import re
import time
from pathlib import Path

import paramiko


REMOTE_PROBE = r'''
import base64
import io
import json
import shutil
import sys
import time
import urllib.error
import urllib.request


def request(url, *, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return {
                "ok": True,
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "content_length": len(body),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "final_url": response.geturl(),
            }, body
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        return {
            "ok": False,
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type", ""),
            "content_length": len(body),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }, body
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "content_type": "",
            "content_length": 0,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }, b""


def jpeg_dimensions(data):
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in range(0xC0, 0xC4):
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return [width, height]
        index += length
    return None


config = json.loads(sys.stdin.read())
video_id = config["video_id"]
user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
results = {
    "youtube_page": {},
    "thumbnail": {},
    "collector_runtime": {},
    "vision_model": {},
}

thumbnail_bytes = b""
model_image_source = "youtube_thumbnail"
if config.get("skip_source_probes"):
    results["youtube_page"] = {"skipped": True}
    results["thumbnail"] = {"skipped": True}
else:
    page_url = f"https://www.youtube.com/watch?v={video_id}"
    page_result, page_body = request(page_url, headers=headers, timeout=30)
    page_text = page_body[:262144].decode("utf-8", errors="ignore").lower()
    page_result["has_youtube_html"] = "youtube" in page_text and "<html" in page_text
    page_result["has_playability_data"] = "playabilitystatus" in page_text
    page_result["looks_blocked"] = any(marker in page_text for marker in (
        "unusual traffic", "before you continue to youtube", "www.google.com/sorry",
    ))
    results["youtube_page"] = page_result

    for variant in ("maxresdefault", "hqdefault"):
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/{variant}.jpg"
        thumbnail_result, candidate = request(thumbnail_url, headers=headers, timeout=30)
        dimensions = jpeg_dimensions(candidate)
        thumbnail_result.update({
            "variant": variant,
            "url": thumbnail_url,
            "jpeg_valid": dimensions is not None,
            "dimensions": dimensions,
        })
        if thumbnail_result["ok"] and dimensions:
            thumbnail_bytes = candidate
            results["thumbnail"] = thumbnail_result
            break
        results["thumbnail"] = thumbnail_result

if not thumbnail_bytes and config.get("fallback_image_base64"):
    thumbnail_bytes = base64.b64decode(config["fallback_image_base64"])
    model_image_source = "provided_probe_image"

yt_dlp_path = shutil.which("yt-dlp") or shutil.which("yt-dlp-nightly")
try:
    import yt_dlp  # noqa: F401
    yt_dlp_module = True
except Exception:
    yt_dlp_module = False
results["collector_runtime"] = {
    "yt_dlp_executable": bool(yt_dlp_path),
    "yt_dlp_python_module": yt_dlp_module,
}

profile = config.get("profile") or {}
if not thumbnail_bytes:
    results["vision_model"] = {"ok": False, "error_type": "thumbnail_unavailable"}
elif not all(profile.get(key) for key in ("api_base", "api_key", "model")):
    results["vision_model"] = {"ok": False, "error_type": "profile_incomplete"}
else:
    endpoint = str(profile["api_base"]).rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions"
    prompt = (
        "Inspect this YouTube thumbnail using only visible evidence. Return only JSON: "
        '{"overall_risk":"safe|review|risk|unknown","risk_tags":[],"summary":"short",'
        '"evidence":"visible evidence","confidence":0.0}'
    )
    model_payload = {
        "model": profile["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a conservative cover-image compliance reviewer."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(thumbnail_bytes).decode("ascii")
                }},
            ]},
        ],
    }
    model_headers = {
        "Authorization": "Bearer " + profile["api_key"],
        "Content-Type": "application/json",
    }
    model_result, model_body = request(
        endpoint,
        data=json.dumps(model_payload, ensure_ascii=False).encode("utf-8"),
        headers=model_headers,
        timeout=120,
    )
    model_result["image_source"] = model_image_source
    model_result["model"] = profile["model"]
    model_result["api_base"] = profile["api_base"]
    try:
        response_payload = json.loads(model_body.decode("utf-8"))
        content = response_payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        text = str(content).strip().replace("```json", "").replace("```", "").strip()
        start, end = text.find("{"), text.rfind("}")
        parsed = json.loads(text[start:end + 1])
        required = {"overall_risk", "risk_tags", "summary", "evidence", "confidence"}
        model_result["structured_response_valid"] = isinstance(parsed, dict) and required <= set(parsed)
        model_result["overall_risk"] = parsed.get("overall_risk") if isinstance(parsed, dict) else None
        model_result["confidence"] = parsed.get("confidence") if isinstance(parsed, dict) else None
    except Exception as exc:
        model_result["structured_response_valid"] = False
        model_result["parse_error"] = f"{type(exc).__name__}: {exc}"
        if not model_result.get("ok"):
            try:
                error_payload = json.loads(model_body.decode("utf-8"))
                model_result["provider_error"] = str(error_payload.get("error") or "")[:500]
            except Exception:
                pass
    results["vision_model"] = model_result

print(json.dumps(results, ensure_ascii=False))
'''


def _ascii_value(resource_file: Path, line_number: int) -> str:
    lines = resource_file.read_bytes().splitlines()
    if line_number >= len(lines):
        raise ValueError(f"resource file has no line {line_number + 1}")
    matches = re.findall(rb"[A-Za-z0-9._-]{4,}", lines[line_number])
    if not matches:
        raise ValueError(f"resource file line {line_number + 1} has no ASCII credential value")
    return matches[-1].decode("ascii")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe cloud cover-monitor prerequisites without persisting secrets.")
    parser.add_argument("--resource-file", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--video-id", default="d4kL7VHzkZA")
    parser.add_argument("--fallback-image", type=Path)
    parser.add_argument("--model-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host = _ascii_value(args.resource_file, 2)
    username = _ascii_value(args.resource_file, 3)
    password = _ascii_value(args.resource_file, 4)
    model_config = json.loads(args.model_config.read_text(encoding="utf-8"))
    profiles = list((model_config.get("llm") or {}).get("profiles") or [])
    profile = next(
        (
            item
            for item in profiles
            if item.get("enabled", True)
            and item.get("api_base")
            and item.get("api_key")
            and item.get("model")
        ),
        {},
    )
    fallback_image_base64 = ""
    if args.fallback_image:
        fallback_image_base64 = base64.b64encode(args.fallback_image.read_bytes()).decode("ascii")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=username, password=password, timeout=20)
    try:
        encoded_probe = base64.b64encode(REMOTE_PROBE.encode("utf-8")).decode("ascii")
        command = f"python3 -c \"import base64;exec(base64.b64decode('{encoded_probe}'))\""
        stdin, stdout, stderr = client.exec_command(command, timeout=180)
        stdin.write(json.dumps({
            "video_id": args.video_id,
            "profile": profile,
            "fallback_image_base64": fallback_image_base64,
            "skip_source_probes": args.model_only,
        }, ensure_ascii=False))
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise RuntimeError(f"remote probe failed ({exit_status}): {error[-1000:]}")
        result = json.loads(output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
