from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_DB = "data/novel_similarity_v2.sqlite3"
DEFAULT_ENDPOINT = "https://ai.gitee.com/v1/embeddings"
DEFAULT_MODEL = "Qwen3-Embedding-8B"
DEFAULT_SOURCE = "semantic_chunks"


class SweepError(RuntimeError):
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
        raise SweepError(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise SweepError(f"Request failed: {exc}") from exc


def fetch_text_rows(
    db_path: str,
    source: str,
    limit: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        if source == "semantic_chunks":
            sql = """
                SELECT chunk_uid, chapter_uid, content
                  FROM semantic_chunks
                 WHERE content IS NOT NULL
                   AND content != ''
                 ORDER BY chunk_uid
                 LIMIT ?
            """
            rows = cur.execute(sql, (limit,)).fetchall()
            return [
                {
                    "row_id": row[0],
                    "chapter_uid": row[1],
                    "text": row[2][:max_chars] if max_chars > 0 else row[2],
                }
                for row in rows
            ]
        if source == "chapter_contents":
            sql = """
                SELECT chapter_uid, content_clean
                  FROM chapter_contents
                 WHERE content_clean IS NOT NULL
                   AND content_clean != ''
                 ORDER BY chapter_uid
                 LIMIT ?
            """
            rows = cur.execute(sql, (limit,)).fetchall()
            return [
                {
                    "row_id": row[0],
                    "chapter_uid": row[0],
                    "text": row[1][:max_chars] if max_chars > 0 else row[1],
                }
                for row in rows
            ]
        raise SweepError(f"Unsupported source: {source}")
    finally:
        conn.close()


def chunked_rows(rows: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    return [rows[start : start + batch_size] for start in range(0, len(rows), batch_size)]


def run_one_batch(
    endpoint: str,
    token: str,
    model: str,
    dimensions: int,
    encoding_format: str,
    timeout: int,
    batch_index: int,
    batch: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "model": model,
        "input": [item["text"] for item in batch],
        "encoding_format": encoding_format,
        "dimensions": dimensions,
    }
    started = time.perf_counter()
    try:
        resp = http_json(endpoint, token, payload, timeout)
        elapsed = time.perf_counter() - started
        data = resp.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise SweepError(f"Unexpected response size for batch {batch_index}")
        dims = len(data[0].get("embedding", [])) if data else 0
        if dims <= 0:
            raise SweepError(f"Unexpected embedding payload for batch {batch_index}")
        prompt_tokens = int(resp.get("usage", {}).get("prompt_tokens", 0) or 0)
        return {
            "batch_index": batch_index,
            "ok": True,
            "batch_size": len(batch),
            "elapsed_seconds": elapsed,
            "prompt_tokens": prompt_tokens,
            "dimensions": dims,
            "error": None,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return {
            "batch_index": batch_index,
            "ok": False,
            "batch_size": len(batch),
            "elapsed_seconds": elapsed,
            "prompt_tokens": 0,
            "dimensions": 0,
            "error": str(exc),
        }


def parse_csv_ints(value: str) -> list[int]:
    items = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        items.append(int(part))
    if not items:
        raise ValueError("Expected at least one integer value")
    return items


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sweep Gitee Embedding API batch size and concurrency limits."
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--source",
        choices=["semantic_chunks", "chapter_contents"],
        default=DEFAULT_SOURCE,
    )
    parser.add_argument("--limit", type=int, default=96)
    parser.add_argument("--max-chars", type=int, default=800)
    parser.add_argument("--batch-sizes", default="8,12,16,24")
    parser.add_argument("--concurrencies", default="1,2,4")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--encoding-format", default="float")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--token", default=None)
    parser.add_argument("--output-json", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token or os.environ.get("GITEE_AI_TOKEN")
    if not token:
        print("Missing token. Set env GITEE_AI_TOKEN or pass --token.", file=sys.stderr)
        return 2

    batch_sizes = parse_csv_ints(args.batch_sizes)
    concurrencies = parse_csv_ints(args.concurrencies)
    rows = fetch_text_rows(args.db, args.source, args.limit, args.max_chars)
    if not rows:
        print("No text rows found.", file=sys.stderr)
        return 1

    combo_results: list[dict[str, Any]] = []
    for batch_size in batch_sizes:
        batches = chunked_rows(rows, batch_size)
        for concurrency in concurrencies:
            started = time.perf_counter()
            run_results: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [
                    executor.submit(
                        run_one_batch,
                        args.endpoint,
                        token,
                        args.model,
                        args.dimensions,
                        args.encoding_format,
                        args.timeout,
                        idx,
                        batch,
                    )
                    for idx, batch in enumerate(batches, start=1)
                ]
                for future in as_completed(futures):
                    run_results.append(future.result())
            wall_elapsed = time.perf_counter() - started

            ok_results = [item for item in run_results if item["ok"]]
            err_results = [item for item in run_results if not item["ok"]]
            ok_vectors = sum(item["batch_size"] for item in ok_results)
            ok_prompt_tokens = sum(item["prompt_tokens"] for item in ok_results)
            dims = ok_results[0]["dimensions"] if ok_results else 0
            summary = {
                "source": args.source,
                "batch_size": batch_size,
                "concurrency": concurrency,
                "requested_rows": len(rows),
                "requested_batches": len(batches),
                "ok_batches": len(ok_results),
                "failed_batches": len(err_results),
                "ok_vectors": ok_vectors,
                "failed_vectors": sum(item["batch_size"] for item in err_results),
                "dimensions": dims,
                "total_wall_seconds": round(wall_elapsed, 4),
                "avg_vectors_per_second_wall": round(ok_vectors / wall_elapsed, 4) if wall_elapsed > 0 else None,
                "avg_seconds_per_vector_wall": round(wall_elapsed / ok_vectors, 4) if ok_vectors > 0 else None,
                "avg_prompt_tokens_per_vector": round(ok_prompt_tokens / ok_vectors, 2) if ok_vectors > 0 else None,
                "errors": [item["error"] for item in err_results[:5]],
            }
            combo_results.append(summary)
            print(json.dumps(summary, ensure_ascii=False))

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "config": {
                "source": args.source,
                "limit": len(rows),
                "max_chars": args.max_chars,
                "batch_sizes": batch_sizes,
                "concurrencies": concurrencies,
                "model": args.model,
                "dimensions": args.dimensions,
            },
            "results": combo_results,
        }
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved sweep report to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
