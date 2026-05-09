from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_DB = "data/novel_similarity_v2.sqlite3"
DEFAULT_ENDPOINT = "https://ai.gitee.com/v1/embeddings"
DEFAULT_MODEL = "Qwen3-Embedding-8B"
DEFAULT_SOURCE = "semantic_chunks"


class BenchmarkError(RuntimeError):
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
        raise BenchmarkError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise BenchmarkError(f"Request failed for {url}: {exc}") from exc


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
        raise BenchmarkError(f"Unsupported source: {source}")
    finally:
        conn.close()


def chunked_texts(rows: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    for start in range(0, len(rows), batch_size):
        batches.append(rows[start : start + batch_size])
    return batches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Gitee Embedding API on real novel text samples."
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--source",
        choices=["semantic_chunks", "chapter_contents"],
        default=DEFAULT_SOURCE,
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--max-chars", type=int, default=800)
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

    if args.limit <= 0:
        print("--limit must be > 0", file=sys.stderr)
        return 2
    if args.batch_size <= 0:
        print("--batch-size must be > 0", file=sys.stderr)
        return 2

    token = args.token or os.environ.get("GITEE_AI_TOKEN")
    if not token:
        print("Missing token. Set env GITEE_AI_TOKEN or pass --token.", file=sys.stderr)
        return 2

    rows = fetch_text_rows(
        db_path=args.db,
        source=args.source,
        limit=args.limit,
        max_chars=args.max_chars,
    )
    if not rows:
        print("No text rows found.", file=sys.stderr)
        return 1

    batches = chunked_texts(rows, args.batch_size)
    batch_summaries: list[dict[str, Any]] = []
    total_vectors = 0
    total_prompt_tokens = 0
    total_elapsed = 0.0
    vector_dimensions = None

    for index, batch in enumerate(batches, start=1):
        texts = [item["text"] for item in batch]
        payload = {
            "model": args.model,
            "input": texts,
            "encoding_format": args.encoding_format,
            "dimensions": args.dimensions,
        }
        started = time.perf_counter()
        resp = http_json(
            url=args.endpoint,
            token=token,
            payload=payload,
            timeout=args.timeout,
        )
        elapsed = time.perf_counter() - started

        data = resp.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise BenchmarkError(
                f"Unexpected response size for batch {index}: {json.dumps(resp, ensure_ascii=False)}"
            )

        dims = len(data[0].get("embedding", [])) if data else 0
        if dims <= 0:
            raise BenchmarkError(
                f"Unexpected embedding payload for batch {index}: {json.dumps(resp, ensure_ascii=False)}"
            )
        vector_dimensions = dims

        prompt_tokens = resp.get("usage", {}).get("prompt_tokens", 0) or 0
        total_vectors += len(texts)
        total_prompt_tokens += int(prompt_tokens)
        total_elapsed += elapsed

        batch_summary = {
            "batch_index": index,
            "batch_size": len(texts),
            "elapsed_seconds": round(elapsed, 4),
            "vectors_per_second": round(len(texts) / elapsed, 4) if elapsed > 0 else None,
            "prompt_tokens": prompt_tokens,
            "sample_row_ids": [item["row_id"] for item in batch[:3]],
        }
        batch_summaries.append(batch_summary)
        print(json.dumps(batch_summary, ensure_ascii=False))

    summary = {
        "ok": True,
        "source": args.source,
        "model": args.model,
        "dimensions": vector_dimensions,
        "requested_dimensions": args.dimensions,
        "rows": len(rows),
        "batch_size": args.batch_size,
        "batches": len(batches),
        "max_chars": args.max_chars,
        "total_prompt_tokens": total_prompt_tokens,
        "total_elapsed_seconds": round(total_elapsed, 4),
        "avg_seconds_per_vector": round(total_elapsed / total_vectors, 4),
        "avg_vectors_per_second": round(total_vectors / total_elapsed, 4) if total_elapsed > 0 else None,
        "avg_prompt_tokens_per_vector": round(total_prompt_tokens / total_vectors, 2) if total_vectors > 0 else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "summary": summary,
            "batches": batch_summaries,
            "sample_rows": [
                {
                    "row_id": row["row_id"],
                    "chapter_uid": row["chapter_uid"],
                    "text_len": len(row["text"]),
                    "text_preview": row["text"][:120],
                }
                for row in rows[:10]
            ],
        }
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved benchmark report to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
