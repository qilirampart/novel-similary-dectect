from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterable
from urllib import error, parse, request

from v2_common import connect_db


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.gitee_embedding_client_v1 import (  # noqa: E402
    DEFAULT_GITEE_API_ENDPOINT,
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    gitee_embed_texts,
    probe_gitee_embedding_dimension,
    resolve_gitee_token,
)


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_MODEL = "Qwen3-Embedding-8B"
DEFAULT_CHAPTER_COLLECTION = "novel_chapter_embeddings"
DEFAULT_CHUNK_COLLECTION = "novel_semantic_chunk_embeddings_qwen3_8b_1024_v1"
DEFAULT_EMBEDDING_BACKEND = "gitee_api"


class SyncError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        raise SyncError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise SyncError(f"Request failed for {url}: {exc}") from exc


def chunked(items: list[tuple[Any, ...]], batch_size: int) -> Iterable[list[tuple[Any, ...]]]:
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def preview_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def probe_embedding_dimension(ollama_url: str, model: str) -> int:
    payload = {
        "model": model,
        "input": "中文小说语义向量维度探针",
    }
    resp = http_json("POST", f"{ollama_url}/api/embed", payload)
    embeddings = resp.get("embeddings")
    if not isinstance(embeddings, list) or not embeddings:
        raise SyncError(f"Unexpected Ollama response: {resp}")
    vector = embeddings[0]
    if not isinstance(vector, list) or not vector:
        raise SyncError(f"Unexpected embedding vector: {resp}")
    return len(vector)


def embed_texts(ollama_url: str, model: str, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    payload = {
        "model": model,
        "input": texts,
    }
    resp = http_json("POST", f"{ollama_url}/api/embed", payload, timeout=600)
    embeddings = resp.get("embeddings")
    if not isinstance(embeddings, list):
        raise SyncError(f"Unexpected Ollama response: {resp}")
    if len(embeddings) != len(texts):
        raise SyncError(
            f"Embedding count mismatch: expected {len(texts)}, got {len(embeddings)}"
        )
    return embeddings


def probe_embedding_dimension_by_backend(
    backend: str,
    ollama_url: str,
    model: str,
    gitee_endpoint: str,
    gitee_token: str,
    gitee_dimensions: int,
) -> int:
    if backend == "ollama":
        return probe_embedding_dimension(ollama_url, model)
    if backend == "gitee_api":
        try:
            return probe_gitee_embedding_dimension(
                endpoint=gitee_endpoint,
                token=resolve_gitee_token(gitee_token),
                model=model,
                dimensions=gitee_dimensions,
            )
        except GiteeEmbeddingError as exc:
            raise SyncError(str(exc)) from exc
    raise SyncError(f"Unsupported embedding backend: {backend!r}")


def embed_texts_by_backend(
    backend: str,
    ollama_url: str,
    model: str,
    texts: list[str],
    gitee_endpoint: str,
    gitee_token: str,
    gitee_dimensions: int,
) -> list[list[float]]:
    if backend == "ollama":
        return embed_texts(ollama_url, model, texts)
    if backend == "gitee_api":
        try:
            return gitee_embed_texts(
                endpoint=gitee_endpoint,
                token=resolve_gitee_token(gitee_token),
                model=model,
                texts=texts,
                dimensions=gitee_dimensions,
                timeout=600,
            )
        except GiteeEmbeddingError as exc:
            raise SyncError(str(exc)) from exc
    raise SyncError(f"Unsupported embedding backend: {backend!r}")


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
        raise SyncError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise SyncError(f"Request failed for {url}: {exc}") from exc


def ensure_collection(qdrant_url: str, name: str, vector_size: int) -> dict[str, Any]:
    existing = get_collection_info(qdrant_url, name)
    if existing is not None:
        try:
            current_size = existing["result"]["config"]["params"]["vectors"]["size"]
        except KeyError as exc:
            raise SyncError(f"Unexpected collection payload for {name}: {existing}") from exc
        if current_size != vector_size:
            raise SyncError(
                f"Collection {name} already exists but vector size is {current_size}, expected {vector_size}."
            )
        return existing

    payload = {
        "vectors": {
            "size": vector_size,
            "distance": "Cosine",
        },
        "on_disk_payload": True,
    }
    http_json("PUT", f"{qdrant_url}/collections/{name}", payload)
    created = get_collection_info(qdrant_url, name)
    if created is None:
        raise SyncError(f"Collection {name} was not created successfully.")
    return created


def upsert_points(qdrant_url: str, collection: str, points: list[dict[str, Any]]) -> None:
    if not points:
        return
    quoted_collection = parse.quote(collection, safe="")
    url = f"{qdrant_url}/collections/{quoted_collection}/points?wait=true"
    http_json("PUT", url, {"points": points}, timeout=600)


def collection_points_count(collection_info: dict[str, Any]) -> int | None:
    result = collection_info.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("points_count")
    return value if isinstance(value, int) else None


def ensure_chapter_sync_state_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chapter_embedding_sync_state (
            chapter_uid INTEGER NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (chapter_uid, collection_name, embedding_model, embedding_dim)
        )
        """
    )


def ensure_chunk_sync_state_table(conn: Any) -> None:
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


def write_progress(progress_json: str, payload: dict[str, Any]) -> None:
    if not progress_json:
        return
    path = Path(progress_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def pct(processed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return processed / total


def estimate_eta_seconds(started_at: float, processed: int, total: int) -> int | None:
    if processed <= 0 or total <= 0 or processed >= total:
        return 0
    elapsed = time.time() - started_at
    rate = elapsed / processed
    remaining = total - processed
    return int(rate * remaining)


def build_chapter_sql(
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    resume_enabled: bool,
    collection: str,
    model: str,
    vector_size: int,
    start_after_chapter_uid: int,
) -> tuple[str, list[object]]:
    sql = """
        SELECT
            c.chapter_uid,
            c.dataset_key,
            c.book_uid,
            b.book_ext_id,
            b.book_name,
            c.chapter_ext_id,
            c.chapter_order,
            c.chapter_name,
            c.content_sha256,
            c.char_count_clean,
            cc.content_clean
          FROM chapters c
          JOIN books b ON b.book_uid = c.book_uid
          JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
         WHERE cc.content_clean IS NOT NULL
           AND cc.content_clean != ''
    """
    params: list[object] = []
    if dataset_key:
        sql += " AND c.dataset_key = ?"
        params.append(dataset_key)
    if start_after_chapter_uid > 0:
        sql += " AND c.chapter_uid > ?"
        params.append(start_after_chapter_uid)
    if limit > 0:
        chapter_scope_sql, chapter_scope_params = build_chapter_scope_sql(
            dataset_key=dataset_key,
            canonical_only=canonical_only,
            limit=limit,
            start_after_chapter_uid=start_after_chapter_uid,
        )
        sql += f" AND c.chapter_uid IN ({chapter_scope_sql})"
        params.extend(chapter_scope_params)
    if canonical_only:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM chapter_exact_dedup_members m
                 WHERE m.chapter_uid = c.chapter_uid
                   AND m.is_canonical = 0
           )
        """
    if resume_enabled:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM chapter_embedding_sync_state s
                 WHERE s.chapter_uid = c.chapter_uid
                   AND s.collection_name = ?
                   AND s.embedding_model = ?
                   AND s.embedding_dim = ?
           )
        """
        params.extend([collection, model, vector_size])
    sql += " ORDER BY c.chapter_uid"
    return sql, params


def build_chapter_scope_sql(
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    start_after_chapter_uid: int,
) -> tuple[str, list[object]]:
    sql = """
        SELECT c.chapter_uid
          FROM chapters c
          JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
         WHERE cc.content_clean IS NOT NULL
           AND cc.content_clean != ''
    """
    params: list[object] = []
    if dataset_key:
        sql += " AND c.dataset_key = ?"
        params.append(dataset_key)
    if start_after_chapter_uid > 0:
        sql += " AND c.chapter_uid > ?"
        params.append(start_after_chapter_uid)
    if canonical_only:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM chapter_exact_dedup_members m
                 WHERE m.chapter_uid = c.chapter_uid
                   AND m.is_canonical = 0
           )
        """
    sql += " ORDER BY c.chapter_uid"
    if limit > 0:
        sql += f" LIMIT {limit}"
    return sql, params


def build_chunk_sql(
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    resume_enabled: bool,
    collection: str,
    model: str,
    vector_size: int,
    start_after_chunk_uid: int,
    align_chapter_scope_limit: int,
    chapter_scope_start_after_chapter_uid: int,
) -> tuple[str, list[object]]:
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
         WHERE sc.content IS NOT NULL
           AND sc.content != ''
    """
    params: list[object] = []
    if dataset_key:
        sql += " AND c.dataset_key = ?"
        params.append(dataset_key)
    if start_after_chunk_uid > 0:
        sql += " AND sc.chunk_uid > ?"
        params.append(start_after_chunk_uid)
    if align_chapter_scope_limit > 0:
        chapter_scope_sql, chapter_scope_params = build_chapter_scope_sql(
            dataset_key=dataset_key,
            canonical_only=canonical_only,
            limit=align_chapter_scope_limit,
            start_after_chapter_uid=chapter_scope_start_after_chapter_uid,
        )
        sql += f" AND sc.chapter_uid IN ({chapter_scope_sql})"
        params.extend(chapter_scope_params)
    if canonical_only:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM chapter_exact_dedup_members m
                 WHERE m.chapter_uid = c.chapter_uid
                   AND m.is_canonical = 0
           )
        """
    if resume_enabled:
        sql += """
           AND NOT EXISTS (
                SELECT 1
                  FROM semantic_chunk_embedding_sync_state s
                 WHERE s.chunk_uid = sc.chunk_uid
                   AND s.collection_name = ?
                   AND s.embedding_model = ?
                   AND s.embedding_dim = ?
           )
        """
        params.extend([collection, model, vector_size])
    sql += " ORDER BY sc.chunk_uid"
    if limit > 0 and align_chapter_scope_limit <= 0:
        sql += f" LIMIT {limit}"
    return sql, params


def sync_chapters(
    db_path: str,
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    batch_size: int,
    ollama_url: str,
    qdrant_url: str,
    model: str,
    collection: str,
    vector_size: int,
    embedding_backend: str,
    gitee_endpoint: str,
    gitee_token: str,
    gitee_dimensions: int,
    progress_json: str,
    resume_enabled: bool,
    start_after_chapter_uid: int,
) -> int:
    ensure_collection(qdrant_url, collection, vector_size)

    sql, params = build_chapter_sql(
        dataset_key=dataset_key,
        canonical_only=canonical_only,
        limit=limit,
        resume_enabled=resume_enabled,
        collection=collection,
        model=model,
        vector_size=vector_size,
        start_after_chapter_uid=start_after_chapter_uid,
    )
    conn = connect_db(db_path)
    conn.row_factory = None
    upserted_total = 0
    try:
        ensure_chapter_sync_state_table(conn)
        total_rows = int(conn.execute(f"SELECT COUNT(1) FROM ({sql})", params).fetchone()[0])
        started_at = time.time()
        processed_rows = 0
        print(
            f"START target=chapters total_rows={total_rows} batch_size={batch_size} "
            f"resume_enabled={int(resume_enabled)} start_after_chapter_uid={start_after_chapter_uid}"
        )
        write_progress(
            progress_json,
            {
                "status": "running",
                "phase": "chapters",
                "dataset_key": dataset_key,
                "model": model,
                "collection": collection,
                "resume_enabled": resume_enabled,
                "start_after_chapter_uid": start_after_chapter_uid,
                "total_rows": total_rows,
                "processed_rows": 0,
                "upserted_total": 0,
                "updated_at_utc": utc_now_iso(),
            },
        )
        cursor = conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            texts = [row[10] for row in rows]
            embeddings = embed_texts_by_backend(
                backend=embedding_backend,
                ollama_url=ollama_url,
                model=model,
                texts=texts,
                gitee_endpoint=gitee_endpoint,
                gitee_token=gitee_token,
                gitee_dimensions=gitee_dimensions,
            )

            points = []
            for row, vector in zip(rows, embeddings):
                (
                    chapter_uid,
                    row_dataset_key,
                    book_uid,
                    book_ext_id,
                    book_name,
                    chapter_ext_id,
                    chapter_order,
                    chapter_name,
                    content_sha256,
                    char_count_clean,
                    content_clean,
                ) = row
                points.append(
                    {
                        "id": chapter_uid,
                        "vector": vector,
                        "payload": {
                            "entity_type": "chapter",
                            "dataset_key": row_dataset_key,
                            "book_uid": book_uid,
                            "book_ext_id": book_ext_id,
                            "book_name": book_name,
                            "chapter_uid": chapter_uid,
                            "chapter_ext_id": chapter_ext_id,
                            "chapter_order": chapter_order,
                            "chapter_name": chapter_name,
                            "char_count_clean": char_count_clean,
                            "embedding_model": model,
                            "embedding_dim": vector_size,
                            "content_sha256": content_sha256,
                            "text_preview": preview_text(content_clean),
                        },
                    }
                )

            upsert_points(qdrant_url, collection, points)
            synced_at = utc_now_iso()
            conn.executemany(
                """
                INSERT INTO chapter_embedding_sync_state(
                    chapter_uid, collection_name, embedding_model, embedding_dim, synced_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chapter_uid, collection_name, embedding_model, embedding_dim) DO UPDATE SET
                    synced_at=excluded.synced_at
                """,
                [(int(row[0]), collection, model, vector_size, synced_at) for row in rows],
            )
            conn.commit()
            processed_rows += len(rows)
            upserted_total += len(points)
            current_pct = pct(processed_rows, total_rows)
            eta_seconds = estimate_eta_seconds(started_at, processed_rows, total_rows)
            print(
                f"PROGRESS target=chapters "
                f"processed_rows={processed_rows} "
                f"total_rows={total_rows} "
                f"pct={current_pct:.4f} "
                f"upserted_total={upserted_total} "
                f"last_batch={len(points)}"
            )
            write_progress(
                progress_json,
                {
                    "status": "running",
                    "phase": "chapters",
                    "dataset_key": dataset_key,
                    "model": model,
                    "collection": collection,
                    "resume_enabled": resume_enabled,
                    "start_after_chapter_uid": start_after_chapter_uid,
                    "total_rows": total_rows,
                    "processed_rows": processed_rows,
                    "processed_pct": round(current_pct, 6),
                    "upserted_total": upserted_total,
                    "last_batch_rows": len(points),
                    "elapsed_seconds": int(time.time() - started_at),
                    "eta_seconds": eta_seconds,
                    "updated_at_utc": utc_now_iso(),
                },
            )
    finally:
        conn.close()

    write_progress(
        progress_json,
        {
            "status": "completed",
            "phase": "chapters",
            "dataset_key": dataset_key,
            "model": model,
            "collection": collection,
            "resume_enabled": resume_enabled,
            "start_after_chapter_uid": start_after_chapter_uid,
            "upserted_total": upserted_total,
            "updated_at_utc": utc_now_iso(),
        },
    )

    return upserted_total


def sync_chunks(
    db_path: str,
    dataset_key: str,
    canonical_only: bool,
    limit: int,
    batch_size: int,
    ollama_url: str,
    qdrant_url: str,
    model: str,
    collection: str,
    vector_size: int,
    embedding_backend: str,
    gitee_endpoint: str,
    gitee_token: str,
    gitee_dimensions: int,
    progress_json: str,
    resume_enabled: bool,
    start_after_chunk_uid: int,
    align_chapter_scope_limit: int,
    chapter_scope_start_after_chapter_uid: int,
) -> int:
    ensure_collection(qdrant_url, collection, vector_size)

    sql, params = build_chunk_sql(
        dataset_key=dataset_key,
        canonical_only=canonical_only,
        limit=limit,
        resume_enabled=resume_enabled,
        collection=collection,
        model=model,
        vector_size=vector_size,
        start_after_chunk_uid=start_after_chunk_uid,
        align_chapter_scope_limit=align_chapter_scope_limit,
        chapter_scope_start_after_chapter_uid=chapter_scope_start_after_chapter_uid,
    )
    conn = connect_db(db_path)
    conn.row_factory = None
    upserted_total = 0
    try:
        ensure_chunk_sync_state_table(conn)
        total_rows = int(conn.execute(f"SELECT COUNT(1) FROM ({sql})", params).fetchone()[0])
        started_at = time.time()
        processed_rows = 0
        print(
            f"START target=chunks total_rows={total_rows} batch_size={batch_size} "
            f"resume_enabled={int(resume_enabled)} start_after_chunk_uid={start_after_chunk_uid} "
            f"align_chapter_scope_limit={align_chapter_scope_limit}"
        )
        write_progress(
            progress_json,
            {
                "status": "running",
                "phase": "chunks",
                "dataset_key": dataset_key,
                "model": model,
                "collection": collection,
                "resume_enabled": resume_enabled,
                "start_after_chunk_uid": start_after_chunk_uid,
                "align_chapter_scope_limit": align_chapter_scope_limit,
                "chapter_scope_start_after_chapter_uid": chapter_scope_start_after_chapter_uid,
                "total_rows": total_rows,
                "processed_rows": 0,
                "upserted_total": 0,
                "updated_at_utc": utc_now_iso(),
            },
        )
        cursor = conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            texts = [row[5] for row in rows]
            embeddings = embed_texts_by_backend(
                backend=embedding_backend,
                ollama_url=ollama_url,
                model=model,
                texts=texts,
                gitee_endpoint=gitee_endpoint,
                gitee_token=gitee_token,
                gitee_dimensions=gitee_dimensions,
            )

            points = []
            chunk_ids: list[int] = []
            for row, vector in zip(rows, embeddings):
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
                chunk_ids.append(chunk_uid)
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

            upsert_points(qdrant_url, collection, points)
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
            processed_rows += len(rows)
            upserted_total += len(points)
            current_pct = pct(processed_rows, total_rows)
            eta_seconds = estimate_eta_seconds(started_at, processed_rows, total_rows)
            print(
                f"PROGRESS target=chunks "
                f"processed_rows={processed_rows} "
                f"total_rows={total_rows} "
                f"pct={current_pct:.4f} "
                f"upserted_total={upserted_total} "
                f"last_batch={len(points)}"
            )
            write_progress(
                progress_json,
                {
                    "status": "running",
                    "phase": "chunks",
                    "dataset_key": dataset_key,
                    "model": model,
                    "collection": collection,
                    "resume_enabled": resume_enabled,
                    "start_after_chunk_uid": start_after_chunk_uid,
                    "align_chapter_scope_limit": align_chapter_scope_limit,
                    "chapter_scope_start_after_chapter_uid": chapter_scope_start_after_chapter_uid,
                    "total_rows": total_rows,
                    "processed_rows": processed_rows,
                    "processed_pct": round(current_pct, 6),
                    "upserted_total": upserted_total,
                    "last_batch_rows": len(points),
                    "elapsed_seconds": int(time.time() - started_at),
                    "eta_seconds": eta_seconds,
                    "updated_at_utc": utc_now_iso(),
                },
            )
    finally:
        conn.close()

    write_progress(
        progress_json,
        {
            "status": "completed",
            "phase": "chunks",
            "dataset_key": dataset_key,
            "model": model,
            "collection": collection,
            "resume_enabled": resume_enabled,
            "start_after_chunk_uid": start_after_chunk_uid,
            "align_chapter_scope_limit": align_chapter_scope_limit,
            "chapter_scope_start_after_chapter_uid": chapter_scope_start_after_chapter_uid,
            "upserted_total": upserted_total,
            "updated_at_utc": utc_now_iso(),
        },
    )

    return upserted_total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument(
        "--target",
        choices=("chapters", "chunks", "all"),
        default="all",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit rows per target")
    parser.add_argument("--batch-size", type=int, default=0, help="Fallback batch size when per-target batch size is not set")
    parser.add_argument("--chapter-batch-size", type=int, default=4)
    parser.add_argument("--chunk-batch-size", type=int, default=2)
    parser.add_argument("--canonical-only", action="store_true")
    parser.add_argument("--embedding-backend", default=DEFAULT_EMBEDDING_BACKEND, choices=("ollama", "gitee_api"))
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--gitee-endpoint", default=DEFAULT_GITEE_API_ENDPOINT)
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--gitee-token-env", default=DEFAULT_GITEE_TOKEN_ENV)
    parser.add_argument("--gitee-dimensions", type=int, default=1024)
    parser.add_argument("--chapter-collection", default=DEFAULT_CHAPTER_COLLECTION)
    parser.add_argument("--chunk-collection", default=DEFAULT_CHUNK_COLLECTION)
    parser.add_argument("--progress-json", default="logs/embed_to_qdrant_progress.json")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--start-after-chapter-uid", type=int, default=0)
    parser.add_argument("--start-after-chunk-uid", type=int, default=0)
    parser.add_argument(
        "--align-chunk-scope-to-chapter-limit",
        type=int,
        default=0,
        help="If > 0, when syncing chunks only, restrict chunk rows to all chunks "
        "belonging to the first N chapters in chapter_uid order",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size < 0:
        raise ValueError("--batch-size must be >= 0")
    if args.chapter_batch_size <= 0:
        raise ValueError("--chapter-batch-size must be > 0")
    if args.chunk_batch_size <= 0:
        raise ValueError("--chunk-batch-size must be > 0")
    if args.start_after_chapter_uid < 0:
        raise ValueError("--start-after-chapter-uid must be >= 0")
    if args.start_after_chunk_uid < 0:
        raise ValueError("--start-after-chunk-uid must be >= 0")
    if args.align_chunk_scope_to_chapter_limit < 0:
        raise ValueError("--align-chunk-scope-to-chapter-limit must be >= 0")
    if args.gitee_dimensions <= 0:
        raise ValueError("--gitee-dimensions must be > 0")

    gitee_token = ""
    if args.embedding_backend == "gitee_api":
        try:
            gitee_token = resolve_gitee_token(args.gitee_token, args.gitee_token_env)
        except GiteeEmbeddingError as exc:
            raise ValueError(str(exc)) from exc

    chapter_batch_size = args.chapter_batch_size or args.batch_size or 4
    chunk_batch_size = args.chunk_batch_size or args.batch_size or 2

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
    print(f"OK embedding_backend={args.embedding_backend}")
    print(f"OK embedding_model={args.model}")
    print(f"OK embedding_dim={vector_size}")
    print(f"OK chapter_batch_size={chapter_batch_size}")
    print(f"OK chunk_batch_size={chunk_batch_size}")
    resume_enabled = not args.no_resume

    chapter_total = 0
    chunk_total = 0

    if args.target in {"chapters", "all"}:
        chapter_total = sync_chapters(
            db_path=args.db,
            dataset_key=args.dataset_key,
            canonical_only=args.canonical_only,
            limit=args.limit,
            batch_size=chapter_batch_size,
            ollama_url=args.ollama_url,
            qdrant_url=args.qdrant_url,
            model=args.model,
            collection=args.chapter_collection,
            vector_size=vector_size,
            embedding_backend=args.embedding_backend,
            gitee_endpoint=args.gitee_endpoint,
            gitee_token=gitee_token,
            gitee_dimensions=args.gitee_dimensions,
            progress_json=args.progress_json,
            resume_enabled=resume_enabled,
            start_after_chapter_uid=args.start_after_chapter_uid,
        )

    if args.target in {"chunks", "all"}:
        align_chapter_scope_limit = (
            args.limit if args.target == "all" and args.limit > 0 else 0
        )
        if args.target == "chunks" and args.align_chunk_scope_to_chapter_limit > 0:
            align_chapter_scope_limit = args.align_chunk_scope_to_chapter_limit
        chunk_total = sync_chunks(
            db_path=args.db,
            dataset_key=args.dataset_key,
            canonical_only=args.canonical_only,
            limit=args.limit,
            batch_size=chunk_batch_size,
            ollama_url=args.ollama_url,
            qdrant_url=args.qdrant_url,
            model=args.model,
            collection=args.chunk_collection,
            vector_size=vector_size,
            embedding_backend=args.embedding_backend,
            gitee_endpoint=args.gitee_endpoint,
            gitee_token=gitee_token,
            gitee_dimensions=args.gitee_dimensions,
            progress_json=args.progress_json,
            resume_enabled=resume_enabled,
            start_after_chunk_uid=args.start_after_chunk_uid,
            align_chapter_scope_limit=align_chapter_scope_limit,
            chapter_scope_start_after_chapter_uid=args.start_after_chapter_uid,
        )

    chapter_info = get_collection_info(args.qdrant_url, args.chapter_collection)
    chunk_info = get_collection_info(args.qdrant_url, args.chunk_collection)

    print(f"OK target={args.target}")
    print(f"OK chapter_points_upserted={chapter_total}")
    print(f"OK chunk_points_upserted={chunk_total}")
    print(f"OK chapter_collection={args.chapter_collection}")
    print(f"OK chapter_collection_points={collection_points_count(chapter_info or {})}")
    print(f"OK chunk_collection={args.chunk_collection}")
    print(f"OK chunk_collection_points={collection_points_count(chunk_info or {})}")


if __name__ == "__main__":
    main()
