from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_ENDPOINT = "https://ai.gitee.com/v1/embeddings"
DEFAULT_MODEL = "Qwen3-Embedding-8B"
DEFAULT_TEXT = "这是一个用于测试 Gitee Embedding API 可用性的短句。"


class GiteeEmbeddingError(RuntimeError):
    pass


def http_json(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GiteeEmbeddingError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise GiteeEmbeddingError(f"Request failed for {url}: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Minimal smoke test for Gitee Embedding API."
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--dimensions", type=int, default=None)
    parser.add_argument("--encoding-format", default="float")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--token",
        default=None,
        help="Optional access token. Defaults to env GITEE_AI_TOKEN.",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Optional path to save the raw API response JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    token = args.token or os.environ.get("GITEE_AI_TOKEN")
    if not token:
        print(
            "Missing token. Set env GITEE_AI_TOKEN or pass --token.",
            file=sys.stderr,
        )
        return 2

    payload: dict[str, Any] = {
        "model": args.model,
        "input": args.text,
        "encoding_format": args.encoding_format,
    }
    if args.dimensions is not None:
        payload["dimensions"] = args.dimensions

    try:
        resp = http_json(
            url=args.endpoint,
            token=token,
            payload=payload,
            timeout=args.timeout,
        )
    except GiteeEmbeddingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    data = resp.get("data")
    if not isinstance(data, list) or not data:
        print(
            f"Unexpected response payload: {json.dumps(resp, ensure_ascii=False)}",
            file=sys.stderr,
        )
        return 1

    first = data[0]
    embedding = first.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        print(
            f"Unexpected embedding payload: {json.dumps(resp, ensure_ascii=False)}",
            file=sys.stderr,
        )
        return 1

    summary = {
        "ok": True,
        "model": resp.get("model", args.model),
        "dimensions": len(embedding),
        "prompt_tokens": resp.get("usage", {}).get("prompt_tokens"),
        "total_tokens": resp.get("usage", {}).get("total_tokens"),
        "first_values": embedding[:5],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(resp, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Saved raw response to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
