from __future__ import annotations

import argparse
import json
import math
import socket
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from gitee_embedding_client_v1 import (
    DEFAULT_GITEE_API_ENDPOINT,
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    gitee_embed_texts,
    probe_gitee_embedding_dimension,
    resolve_gitee_token,
)
from v2_common import connect_db


DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_MODEL = "Qwen3-Embedding-8B"
DEFAULT_COLLECTION = "novel_semantic_chunk_embeddings_gitee_v1"


class BackfillError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def preview_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BackfillError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise BackfillError(f"Request failed for {url}: {exc}") from exc


def get_collection_info(qdrant_url: str, name: str) -> dict[str, Any] | None:
    url = f"{qdrant_url}/collections/{name}"
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")
        raise BackfillError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise BackfillError(f"Request failed for {url}: {exc}") from exc


def ensure_collection(qdrant_url: str, name: str, vector_size: int) -> None:
    existing = get_collection_info(qdrant_url, name)
    if existing is not None:
        current_size = existing["result"]["config"]["params"]["vectors"]["size"]
        if current_size != vector_size:
            raise BackfillError(
                f"Collection {name} vector size mismatch: {current_size} != {vector_size}"
            )
        return

    payload = {
        "vectors": {
            "size": vector_size,
            "distance": "Cosine",
        },
        "on_disk_payload": True,
    }
    http_json("PUT", f"{qdrant_url}/collections/{name}", payload, timeout=300)


def upsert_points(qdrant_url: str, collection: str, points: list[dict[str, Any]]) -> None:
    quoted_collection = parse.quote(collection, safe="")
    url = f"{qdrant_url}/collections/{quoted_collection}/points?wait=true"
    http_json("PUT", url, {"points": points}, timeout=600)


def ensure_chunk_sync_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_chunk_embedding_sync_state (
            chunk_uid INTEGER NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (chunk_uid, collection_name, embedding_model, embedding_dim)
        )
        """
    )


def build_pending_chunk_filters(
    dataset_key: str,
    canonical_only: bool,
    collection: str,
    model: str,
    vector_size: int,
    start_after_chunk_uid: int,
) -> tuple[str, list[object]]:
    clauses = [
        "sc.content IS NOT NULL",
        "sc.content != ''",
        "sc.chunk_uid > ?",
    ]
    params: list[object] = [start_after_chunk_uid]
    if dataset_key:
        clauses.append("c.dataset_key = ?")
        params.append(dataset_key)
    if canonical_only:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                  FROM chapter_exact_dedup_members m
                 WHERE m.chapter_uid = c.chapter_uid
                   AND m.is_canonical = 0
            )
            """
        )
    clauses.append(
        """
        NOT EXISTS (
            SELECT 1
              FROM semantic_chunk_embedding_sync_state s
             WHERE s.chunk_uid = sc.chunk_uid
               AND s.collection_name = ?
               AND s.embedding_model = ?
               AND s.embedding_dim = ?
        )
        """
    )
    params.extend([collection, model, vector_size])
    where_sql = " WHERE " + " AND ".join(f"({clause.strip()})" for clause in clauses)
    return where_sql, params


def build_chunk_sql(
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    collection: str,
    model: str,
    vector_size: int,
    start_after_chunk_uid: int,
) -> tuple[str, list[object]]:
    where_sql, params = build_pending_chunk_filters(
        dataset_key=dataset_key,
        canonical_only=canonical_only,
        collection=collection,
        model=model,
        vector_size=vector_size,
        start_after_chunk_uid=start_after_chunk_uid,
    )
    sql = """
        SELECT
            sc.chunk_uid,
            sc.chapter_uid,
            sc.chunk_order,
            sc.start_offset,
            sc.end_offset,
            sc.content,
            c.dataset_key,
            c.book_uid,
            b.book_ext_id,
            b.book_name,
            c.chapter_ext_id,
            c.chapter_order,
            c.chapter_name,
            c.content_sha256
          FROM semantic_chunks sc
          JOIN chapters c ON c.chapter_uid = sc.chapter_uid
          JOIN books b ON b.book_uid = c.book_uid
    """
    sql += where_sql
    sql += " ORDER BY sc.chunk_uid"
    if limit > 0:
        sql += f" LIMIT {limit}"
    return sql, params


def fetch_pending_rows(
    db_path: str,
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    collection: str,
    model: str,
    vector_size: int,
    start_after_chunk_uid: int,
) -> list[tuple[Any, ...]]:
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        sql, params = build_chunk_sql(
            dataset_key=dataset_key,
            canonical_only=canonical_only,
            limit=limit,
            collection=collection,
            model=model,
            vector_size=vector_size,
            start_after_chunk_uid=start_after_chunk_uid,
        )
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def count_pending_rows(
    db_path: str,
    dataset_key: str,
    canonical_only: bool,
    collection: str,
    model: str,
    vector_size: int,
    start_after_chunk_uid: int,
) -> int:
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        where_sql, params = build_pending_chunk_filters(
            dataset_key=dataset_key,
            canonical_only=canonical_only,
            collection=collection,
            model=model,
            vector_size=vector_size,
            start_after_chunk_uid=start_after_chunk_uid,
        )
        sql = """
            SELECT COUNT(*)
              FROM semantic_chunks sc
              JOIN chapters c ON c.chapter_uid = sc.chapter_uid
        """
        sql += where_sql
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def build_points(rows: list[tuple[Any, ...]], vectors: list[list[float]], model: str, vector_size: int) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row, vector in zip(rows, vectors):
        (
            chunk_uid,
            chapter_uid,
            chunk_order,
            start_offset,
            end_offset,
            content,
            row_dataset_key,
            book_uid,
            book_ext_id,
            book_name,
            chapter_ext_id,
            chapter_order,
            chapter_name,
            content_sha256,
        ) = row
        points.append(
            {
                "id": chunk_uid,
                "vector": vector,
                "payload": {
                    "entity_type": "semantic_chunk",
                    "dataset_key": row_dataset_key,
                    "book_uid": book_uid,
                    "book_ext_id": book_ext_id,
                    "book_name": book_name,
                    "chapter_uid": chapter_uid,
                    "chapter_ext_id": chapter_ext_id,
                    "chapter_order": chapter_order,
                    "chapter_name": chapter_name,
                    "chunk_uid": chunk_uid,
                    "chunk_order": chunk_order,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                    "embedding_model": model,
                    "embedding_dim": vector_size,
                    "content_sha256": content_sha256,
                    "text_preview": preview_text(content),
                },
            }
        )
    return points


def mark_synced(
    db_path: str,
    rows: list[tuple[Any, ...]],
    collection: str,
    model: str,
    vector_size: int,
) -> None:
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        ensure_chunk_sync_state_table(conn)
        chunk_ids = [int(row[0]) for row in rows]
        conn.executemany(
            """
            UPDATE semantic_chunks
               SET embedding_model = ?,
                   embedding_dim = ?
             WHERE chunk_uid = ?
            """,
            [(model, vector_size, chunk_uid) for chunk_uid in chunk_ids],
        )
        synced_at = utc_now_iso()
        conn.executemany(
            """
            INSERT INTO semantic_chunk_embedding_sync_state(
                chunk_uid, collection_name, embedding_model, embedding_dim, synced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chunk_uid, collection_name, embedding_model, embedding_dim) DO UPDATE SET
                synced_at=excluded.synced_at
            """,
            [(chunk_uid, collection, model, vector_size, synced_at) for chunk_uid in chunk_ids],
        )
        conn.commit()
    finally:
        conn.close()


def write_progress(progress_json: str, payload: dict[str, Any]) -> None:
    if not progress_json:
        return
    path = Path(progress_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_batch_jobs(rows: list[tuple[Any, ...]], batch_size: int) -> list[list[tuple[Any, ...]]]:
    return [rows[start : start + batch_size] for start in range(0, len(rows), batch_size)]


def process_batch(
    endpoint: str,
    token: str,
    model: str,
    dimensions: int,
    batch_rows: list[tuple[Any, ...]],
    qdrant_url: str,
    collection: str,
    db_path: str,
    retry_limit: int,
    retry_sleep_seconds: float,
) -> dict[str, Any]:
    texts = [str(row[5]) for row in batch_rows]
    last_error = ""
    for attempt in range(1, retry_limit + 1):
        try:
            vectors = gitee_embed_texts(
                endpoint=endpoint,
                token=token,
                model=model,
                texts=texts,
                dimensions=dimensions,
                timeout=600,
            )
            points = build_points(batch_rows, vectors, model, dimensions)
            upsert_points(qdrant_url, collection, points)
            mark_synced(db_path, batch_rows, collection, model, dimensions)
            return {
                "ok": True,
                "row_count": len(batch_rows),
                "last_chunk_uid": int(batch_rows[-1][0]),
                "attempts": attempt,
                "error": "",
            }
        except (GiteeEmbeddingError, BackfillError, socket.timeout, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retry_limit:
                time.sleep(retry_sleep_seconds * attempt)
                continue
            return {
                "ok": False,
                "row_count": len(batch_rows),
                "last_chunk_uid": int(batch_rows[-1][0]),
                "attempts": attempt,
                "error": last_error,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retry_limit:
                time.sleep(retry_sleep_seconds * attempt)
                continue
            return {
                "ok": False,
                "row_count": len(batch_rows),
                "last_chunk_uid": int(batch_rows[-1][0]),
                "attempts": attempt,
                "error": last_error,
            }
    return {
        "ok": False,
        "row_count": len(batch_rows),
        "last_chunk_uid": int(batch_rows[-1][0]),
        "attempts": retry_limit,
        "error": last_error or "unknown error",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--canonical-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--start-after-chunk-uid",
        type=int,
        default=0,
        help="Manual partitioning cursor only; normal resume should rely on sync_state.",
    )
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--retry-limit", type=int, default=3)
    parser.add_argument("--retry-sleep-seconds", type=float, default=2.0)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--gitee-endpoint", default=DEFAULT_GITEE_API_ENDPOINT)
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--gitee-token-env", default=DEFAULT_GITEE_TOKEN_ENV)
    parser.add_argument("--progress-json", default="logs/backfill_gitee_chunks_progress.json")
    parser.add_argument("--failures-jsonl", default="logs/backfill_gitee_chunks_failures.jsonl")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")
    if args.concurrency <= 0:
        raise ValueError("--concurrency must be > 0")
    if args.retry_limit <= 0:
        raise ValueError("--retry-limit must be > 0")
    if args.dimensions <= 0:
        raise ValueError("--dimensions must be > 0")

    token = resolve_gitee_token(args.gitee_token, args.gitee_token_env)
    vector_size = probe_gitee_embedding_dimension(
        endpoint=args.gitee_endpoint,
        token=token,
        model=args.model,
        dimensions=args.dimensions,
    )
    if vector_size != args.dimensions:
        raise BackfillError(
            f"Gitee dimensions mismatch: requested {args.dimensions}, got {vector_size}"
        )

    ensure_collection(args.qdrant_url, args.collection, vector_size)
    total_rows = count_pending_rows(
        db_path=args.db,
        dataset_key=args.dataset_key,
        canonical_only=args.canonical_only,
        collection=args.collection,
        model=args.model,
        vector_size=vector_size,
        start_after_chunk_uid=args.start_after_chunk_uid,
    )
    if args.limit > 0:
        total_rows = min(total_rows, args.limit)
    total_jobs = math.ceil(total_rows / args.batch_size) if total_rows > 0 else 0
    failures_path = Path(args.failures_jsonl)
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    progress_lock = threading.Lock()
    started_at = time.time()
    processed_rows = 0
    success_rows = 0
    failed_rows = 0
    completed_jobs = 0
    failed_jobs = 0
    max_chunk_uid_done = args.start_after_chunk_uid
    fetch_cursor_chunk_uid = args.start_after_chunk_uid
    max_pending_jobs = max(args.concurrency * 4, args.concurrency)
    fetch_exhausted = False

    print(f"OK model={args.model}")
    print(f"OK dimensions={vector_size}")
    print(f"OK collection={args.collection}")
    print(f"OK batch_size={args.batch_size}")
    print(f"OK concurrency={args.concurrency}")
    print(f"OK total_rows={total_rows}")
    print(f"OK total_jobs={total_jobs}")
    print(f"OK max_pending_jobs={max_pending_jobs}")

    write_progress(
        args.progress_json,
        {
            "status": "running",
            "model": args.model,
            "dimensions": vector_size,
            "collection": args.collection,
            "batch_size": args.batch_size,
            "concurrency": args.concurrency,
            "total_rows": total_rows,
            "total_jobs": total_jobs,
            "processed_rows": 0,
            "success_rows": 0,
            "failed_rows": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
            "max_chunk_uid_done": args.start_after_chunk_uid,
            "fetch_cursor_chunk_uid": fetch_cursor_chunk_uid,
            "resume_mode": "sync_state",
            "updated_at_utc": utc_now_iso(),
        },
    )

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        pending: dict[Any, list[tuple[Any, ...]]] = {}

        def submit_more_jobs() -> None:
            nonlocal fetch_cursor_chunk_uid, fetch_exhausted
            while not fetch_exhausted and len(pending) < max_pending_jobs:
                jobs_to_fill = max_pending_jobs - len(pending)
                rows_to_fetch = jobs_to_fill * args.batch_size
                remaining_rows = total_rows - processed_rows - sum(len(rows) for rows in pending.values())
                if remaining_rows <= 0:
                    fetch_exhausted = True
                    break
                if args.limit > 0:
                    rows_to_fetch = min(rows_to_fetch, remaining_rows)
                batch_rows = fetch_pending_rows(
                    db_path=args.db,
                    dataset_key=args.dataset_key,
                    canonical_only=args.canonical_only,
                    limit=rows_to_fetch,
                    collection=args.collection,
                    model=args.model,
                    vector_size=vector_size,
                    start_after_chunk_uid=fetch_cursor_chunk_uid,
                )
                if not batch_rows:
                    fetch_exhausted = True
                    break
                fetch_cursor_chunk_uid = int(batch_rows[-1][0])
                for rows in build_batch_jobs(batch_rows, args.batch_size):
                    future = executor.submit(
                        process_batch,
                        args.gitee_endpoint,
                        token,
                        args.model,
                        vector_size,
                        rows,
                        args.qdrant_url,
                        args.collection,
                        args.db,
                        args.retry_limit,
                        args.retry_sleep_seconds,
                    )
                    pending[future] = rows

        submit_more_jobs()

        while pending:
            done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                batch_rows = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "ok": False,
                        "row_count": len(batch_rows),
                        "last_chunk_uid": int(batch_rows[-1][0]),
                        "attempts": args.retry_limit,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                with progress_lock:
                    completed_jobs += 1
                    processed_rows += int(result["row_count"])
                    max_chunk_uid_done = max(max_chunk_uid_done, int(result["last_chunk_uid"]))
                    if result["ok"]:
                        success_rows += int(result["row_count"])
                    else:
                        failed_jobs += 1
                        failed_rows += int(result["row_count"])
                        with failures_path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(
                                {
                                    "row_count": int(result["row_count"]),
                                    "last_chunk_uid": int(result["last_chunk_uid"]),
                                    "attempts": int(result["attempts"]),
                                    "error": str(result["error"]),
                                    "updated_at_utc": utc_now_iso(),
                                    "chunk_uids": [int(row[0]) for row in batch_rows],
                                },
                                ensure_ascii=False,
                            ) + "\n")

                    elapsed = time.time() - started_at
                    vps = success_rows / elapsed if elapsed > 0 else 0.0
                    print(
                        f"PROGRESS completed_jobs={completed_jobs} total_jobs={total_jobs} "
                        f"processed_rows={processed_rows} success_rows={success_rows} failed_rows={failed_rows} "
                        f"failed_jobs={failed_jobs} max_chunk_uid_done={max_chunk_uid_done} "
                        f"vectors_per_second={vps:.4f}"
                    )
                    write_progress(
                        args.progress_json,
                        {
                            "status": "running",
                            "model": args.model,
                            "dimensions": vector_size,
                            "collection": args.collection,
                            "batch_size": args.batch_size,
                            "concurrency": args.concurrency,
                            "total_rows": total_rows,
                            "total_jobs": total_jobs,
                            "processed_rows": processed_rows,
                            "success_rows": success_rows,
                            "failed_rows": failed_rows,
                            "completed_jobs": completed_jobs,
                            "failed_jobs": failed_jobs,
                            "max_chunk_uid_done": max_chunk_uid_done,
                            "fetch_cursor_chunk_uid": fetch_cursor_chunk_uid,
                            "resume_mode": "sync_state",
                            "elapsed_seconds": int(elapsed),
                            "vectors_per_second": round(vps, 4),
                            "updated_at_utc": utc_now_iso(),
                        },
                    )
            submit_more_jobs()

    final_status = "completed" if failed_jobs == 0 else "completed_with_failures"
    elapsed = time.time() - started_at
    vps = success_rows / elapsed if elapsed > 0 else 0.0
    write_progress(
        args.progress_json,
        {
            "status": final_status,
            "model": args.model,
            "dimensions": vector_size,
            "collection": args.collection,
            "batch_size": args.batch_size,
            "concurrency": args.concurrency,
            "total_rows": total_rows,
            "total_jobs": total_jobs,
            "processed_rows": processed_rows,
            "success_rows": success_rows,
            "failed_rows": failed_rows,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
            "max_chunk_uid_done": max_chunk_uid_done,
            "fetch_cursor_chunk_uid": fetch_cursor_chunk_uid,
            "resume_mode": "sync_state",
            "elapsed_seconds": int(elapsed),
            "vectors_per_second": round(vps, 4),
            "updated_at_utc": utc_now_iso(),
        },
    )

    print(f"OK status={final_status}")
    print(f"OK success_rows={success_rows}")
    print(f"OK failed_rows={failed_rows}")
    print(f"OK failed_jobs={failed_jobs}")
    print(f"OK elapsed_seconds={int(elapsed)}")
    print(f"OK vectors_per_second={vps:.4f}")
    print(f"OK progress_json={args.progress_json}")
    print(f"OK failures_jsonl={args.failures_jsonl}")


if __name__ == "__main__":
    main()
