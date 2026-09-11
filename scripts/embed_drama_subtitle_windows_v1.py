from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.v2_common import connect_db  # noqa: E402
from scripts.embed_to_qdrant_v1 import (  # noqa: E402
    DEFAULT_EMBEDDING_BACKEND,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_QDRANT_URL,
    SyncError,
    collection_points_count,
    embed_texts_by_backend,
    ensure_collection,
    estimate_eta_seconds,
    get_collection_info,
    pct,
    probe_embedding_dimension_by_backend,
    upsert_points,
    utc_now_iso,
)
from scripts.gitee_embedding_client_v1 import (  # noqa: E402
    DEFAULT_GITEE_API_ENDPOINT,
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    resolve_gitee_token,
)
from service.drama_subtitle_language import SUPPORTED_LANGUAGE_CODES  # noqa: E402


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_SCHEMA_PATH = "service/drama_subtitle_semantic_schema_v1.sql"
DEFAULT_COLLECTION = "drama_subtitle_window_embeddings_qwen3_4b_2560_v1"
DEFAULT_MODEL = "Qwen3-Embedding-4B"
DEFAULT_GITEE_DIMENSIONS = 2560
DEFAULT_PROGRESS_PATH = "logs/drama_subtitle_embedding_qwen3_4b_2560_progress.json"
DEFAULT_QDRANT_UPSERT_MAX_ATTEMPTS = 4
DEFAULT_QDRANT_UPSERT_RETRY_BACKOFF_SECONDS = 5.0
POINT_NAMESPACE = uuid.UUID("a30b718c-58d2-4ec4-ba26-d38a0102db37")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Embed normalized drama subtitle windows into a dedicated Qdrant collection."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--limit", type=int, default=0, help="0 means all pending windows.")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument(
        "--embedding-concurrency",
        type=int,
        default=4,
        help="Maximum in-flight embedding API requests. Qdrant and SQLite writes remain serial.",
    )
    parser.add_argument("--book-id", default="", help="Only embed one target drama book id.")
    parser.add_argument("--language-code", default="", help="Only embed one language: zh, en, ja or ko.")
    parser.add_argument("--embedding-backend", default="gitee_api", choices=("ollama", "gitee_api"))
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gitee-endpoint", default=DEFAULT_GITEE_API_ENDPOINT)
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--gitee-token-env", default=DEFAULT_GITEE_TOKEN_ENV)
    parser.add_argument("--gitee-dimensions", type=int, default=DEFAULT_GITEE_DIMENSIONS)
    parser.add_argument(
        "--qdrant-upsert-max-attempts",
        type=int,
        default=DEFAULT_QDRANT_UPSERT_MAX_ATTEMPTS,
    )
    parser.add_argument(
        "--qdrant-upsert-retry-backoff-seconds",
        type=float,
        default=DEFAULT_QDRANT_UPSERT_RETRY_BACKOFF_SECONDS,
    )
    parser.add_argument("--progress-json", default=DEFAULT_PROGRESS_PATH)
    parser.add_argument("--force", action="store_true", help="Re-embed selected windows even when sync state exists.")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def write_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_pending_sql(
    *,
    force: bool,
    book_id: str,
    collection: str,
    model: str,
    vector_size: int,
    limit: int,
    language_code: str,
) -> tuple[str, list[object]]:
    sql = """
        SELECT w.window_uid,
               w.book_id,
               w.book_name,
               w.episode_uid,
               w.episode_order,
               w.chapter_id,
               w.line_start,
               w.line_end,
               w.time_start,
               w.time_end,
               w.window_text_preview,
               w.line_count,
               w.char_count,
               w.window_text,
               w.language_code,
               w.language_confidence
          FROM drama_subtitle_windows w
    """
    params: list[object] = []
    conditions: list[str] = []
    if book_id:
        conditions.append("w.book_id = ?")
        params.append(book_id)
    if language_code:
        conditions.append("w.language_code = ?")
        params.append(language_code)
    else:
        # Unknown-language windows cannot be safely routed by the semantic query path.
        placeholders = ", ".join("?" for _ in SUPPORTED_LANGUAGE_CODES)
        conditions.append(f"w.language_code IN ({placeholders})")
        params.extend(sorted(SUPPORTED_LANGUAGE_CODES))
    if not force:
        conditions.append(
            """
            NOT EXISTS (
                SELECT 1
                  FROM drama_subtitle_window_embedding_sync_state s
                 WHERE s.window_uid = w.window_uid
                   AND s.collection_name = ?
                   AND s.embedding_model = ?
                   AND s.embedding_dim = ?
            )
            """
        )
        params.extend([collection, model, vector_size])
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY w.book_id, w.episode_order, w.line_start"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return sql, params


def stable_point_id(window_uid: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, window_uid))


def embed_batch(
    rows: list[tuple[Any, ...]],
    *,
    args: argparse.Namespace,
    gitee_token: str,
) -> list[list[float]]:
    """Run only the remote embedding request in a worker thread."""
    embeddings = embed_texts_by_backend(
        backend=args.embedding_backend,
        ollama_url=args.ollama_url,
        model=args.model,
        texts=[str(row[13]) for row in rows],
        gitee_endpoint=args.gitee_endpoint,
        gitee_token=gitee_token,
        gitee_dimensions=args.gitee_dimensions,
    )
    if len(embeddings) != len(rows):
        raise SyncError(
            f"Embedding API returned {len(embeddings)} vectors for {len(rows)} input windows"
        )
    return embeddings


def is_transient_qdrant_error(exc: SyncError) -> bool:
    message = str(exc).lower()
    if "request failed for" in message:
        return True
    return any(f"http {status}" in message for status in (408, 429, 500, 502, 503, 504))


def upsert_points_with_retry(
    qdrant_url: str,
    collection: str,
    points: list[dict[str, Any]],
    *,
    max_attempts: int = DEFAULT_QDRANT_UPSERT_MAX_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_QDRANT_UPSERT_RETRY_BACKOFF_SECONDS,
) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            upsert_points(qdrant_url, collection, points)
            return
        except SyncError as exc:
            if attempt >= max_attempts or not is_transient_qdrant_error(exc):
                raise
            delay = retry_backoff_seconds * (2 ** (attempt - 1))
            print(
                f"WARN qdrant_upsert_retry attempt={attempt}/{max_attempts} "
                f"delay_seconds={delay:g} error={exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)


def main() -> None:
    args = parse_args()
    if (
        args.limit < 0
        or args.batch_size <= 0
        or args.embedding_concurrency <= 0
        or args.gitee_dimensions <= 0
        or args.qdrant_upsert_max_attempts <= 0
        or args.qdrant_upsert_retry_backoff_seconds < 0
    ):
        raise ValueError(
            "--limit must be >= 0; --batch-size, --embedding-concurrency and "
            "--gitee-dimensions, --qdrant-upsert-max-attempts must be > 0; "
            "--qdrant-upsert-retry-backoff-seconds must be >= 0"
        )
    language_code = args.language_code.strip().lower()
    if language_code and language_code not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(f"unsupported --language-code: {language_code}")

    db_path = resolve_path(args.db)
    schema_path = resolve_path(args.schema)
    progress_path = resolve_path(args.progress_json)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    if not schema_path.exists():
        raise FileNotFoundError(f"schema not found: {schema_path}")

    gitee_token = ""
    if args.embedding_backend == "gitee_api":
        try:
            gitee_token = resolve_gitee_token(args.gitee_token, args.gitee_token_env)
        except GiteeEmbeddingError as exc:
            raise ValueError(str(exc)) from exc

    vector_size = probe_embedding_dimension_by_backend(
        backend=args.embedding_backend,
        ollama_url=args.ollama_url,
        model=args.model,
        gitee_endpoint=args.gitee_endpoint,
        gitee_token=gitee_token,
        gitee_dimensions=args.gitee_dimensions,
    )
    if args.embedding_backend == "gitee_api" and vector_size != args.gitee_dimensions:
        raise SyncError(
            f"Gitee dimensions mismatch: requested {args.gitee_dimensions}, got {vector_size}"
        )
    ensure_collection(args.qdrant_url, args.collection, vector_size)

    conn = connect_db(db_path)
    conn.row_factory = None
    processed_rows = 0
    upserted_total = 0
    started_at = time.time()
    try:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.commit()
        sql, params = build_pending_sql(
            force=args.force,
            book_id=args.book_id.strip(),
            collection=args.collection,
            model=args.model,
            vector_size=vector_size,
            limit=args.limit,
            language_code=language_code,
        )
        total_rows = int(conn.execute(f"SELECT COUNT(1) FROM ({sql})", params).fetchone()[0])
        print(
            f"START total_rows={total_rows} batch_size={args.batch_size} "
            f"embedding_concurrency={args.embedding_concurrency} "
            f"collection={args.collection} language={language_code or 'all'} force={int(args.force)}"
        )
        write_progress(
            progress_path,
            {
                "status": "running",
                "total_rows": total_rows,
                "processed_rows": 0,
                "upserted_total": 0,
                "collection": args.collection,
                "language_code": language_code,
                "embedding_concurrency": args.embedding_concurrency,
                "embedding_model": args.model,
                "embedding_dim": vector_size,
                "updated_at_utc": utc_now_iso(),
            },
        )

        cursor = conn.execute(sql, params)
        pending_batches: deque[tuple[list[tuple[Any, ...]], Future[list[list[float]]]]] = deque()

        def schedule_next_batch(executor: ThreadPoolExecutor) -> bool:
            rows = cursor.fetchmany(args.batch_size)
            if not rows:
                return False
            pending_batches.append(
                (
                    rows,
                    executor.submit(embed_batch, rows, args=args, gitee_token=gitee_token),
                )
            )
            return True

        # Only remote embedding calls run concurrently. A single main-thread consumer
        # serializes Qdrant upserts and SQLite commits to preserve resume consistency.
        with ThreadPoolExecutor(max_workers=args.embedding_concurrency) as executor:
            while len(pending_batches) < args.embedding_concurrency and schedule_next_batch(executor):
                pass

            while pending_batches:
                rows, future = pending_batches.popleft()
                embeddings = future.result()
                points: list[dict[str, Any]] = []
                for row, vector in zip(rows, embeddings):
                    points.append(
                        {
                            "id": stable_point_id(str(row[0])),
                            "vector": vector,
                            "payload": {
                                "entity_type": "drama_subtitle_window",
                                "window_uid": row[0],
                                "book_id": row[1],
                                "book_name": row[2],
                                "episode_uid": row[3],
                                "episode_order": row[4],
                                "chapter_id": row[5],
                                "line_start": row[6],
                                "line_end": row[7],
                                "time_start": row[8],
                                "time_end": row[9],
                                "text_preview": row[10],
                                "line_count": row[11],
                                "char_count": row[12],
                                "language_code": row[14],
                                "language_confidence": row[15],
                                "embedding_model": args.model,
                                "embedding_dim": vector_size,
                            },
                        }
                    )
                upsert_points_with_retry(
                    args.qdrant_url,
                    args.collection,
                    points,
                    max_attempts=args.qdrant_upsert_max_attempts,
                    retry_backoff_seconds=args.qdrant_upsert_retry_backoff_seconds,
                )
                synced_at = utc_now_iso()
                conn.executemany(
                    """
                    INSERT INTO drama_subtitle_window_embedding_sync_state(
                        window_uid, collection_name, embedding_model, embedding_dim, synced_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(window_uid, collection_name, embedding_model, embedding_dim) DO UPDATE SET
                        synced_at=excluded.synced_at
                    """,
                    [
                        (str(row[0]), args.collection, args.model, vector_size, synced_at)
                        for row in rows
                    ],
                )
                conn.commit()
                processed_rows += len(rows)
                upserted_total += len(points)
                progress = {
                    "status": "running",
                    "total_rows": total_rows,
                    "processed_rows": processed_rows,
                    "processed_pct": round(pct(processed_rows, total_rows), 6),
                    "upserted_total": upserted_total,
                    "last_batch_rows": len(points),
                    "elapsed_seconds": int(time.time() - started_at),
                    "eta_seconds": estimate_eta_seconds(started_at, processed_rows, total_rows),
                    "collection": args.collection,
                    "language_code": language_code,
                    "embedding_concurrency": args.embedding_concurrency,
                    "embedding_model": args.model,
                    "embedding_dim": vector_size,
                    "updated_at_utc": utc_now_iso(),
                }
                write_progress(progress_path, progress)
                print(
                    f"PROGRESS processed_rows={processed_rows} total_rows={total_rows} "
                    f"pct={progress['processed_pct']:.4f} upserted_total={upserted_total}"
                )
                schedule_next_batch(executor)
    finally:
        conn.close()

    info = get_collection_info(args.qdrant_url, args.collection)
    write_progress(
        progress_path,
        {
            "status": "completed",
            "processed_rows": processed_rows,
            "upserted_total": upserted_total,
            "collection": args.collection,
            "language_code": language_code,
            "embedding_concurrency": args.embedding_concurrency,
            "embedding_model": args.model,
            "embedding_dim": vector_size,
            "collection_points": collection_points_count(info or {}),
            "updated_at_utc": utc_now_iso(),
        },
    )
    print(f"OK upserted_total={upserted_total}")
    print(f"OK collection={args.collection}")
    print(f"OK collection_points={collection_points_count(info or {})}")


if __name__ == "__main__":
    main()
