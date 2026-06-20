from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCAL_INDEX_DIR = ROOT_DIR / "data" / "semantic_local_index" / "chapter_bge_small_zh_v1_5_all_canonical"


def resolve_default_local_model_path() -> str:
    env_path = os.environ.get("NOVEL_LOCAL_EMBED_MODEL_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    snapshots_dir = Path(r"E:\AI_Models\bge\models--BAAI--bge-small-zh-v1.5\snapshots")
    if snapshots_dir.exists():
        candidates = sorted(path for path in snapshots_dir.iterdir() if path.is_dir())
        if candidates:
            return str(candidates[-1])

    return ""


def clip_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if limit <= 0 or len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _semantic_error(message: str) -> RuntimeError:
    from service.semantic_retrieval import SemanticRetrievalError

    return SemanticRetrievalError(message)


def _require_existing_dir(path_text: str, label: str) -> Path:
    path = Path(path_text)
    if not path.exists():
        raise _semantic_error(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise _semantic_error(f"{label} is not a directory: {path}")
    return path


@lru_cache(maxsize=2)
def load_local_sentence_model(model_path: str, device: str) -> SentenceTransformer:
    if not model_path:
        raise _semantic_error(
            "Local semantic backend requires --semantic-local-model-path or NOVEL_LOCAL_EMBED_MODEL_PATH."
        )
    resolved = _require_existing_dir(model_path, "Local embedding model path")
    return SentenceTransformer(str(resolved), device=device)


def embed_texts_local(
    model_path: str,
    texts: list[str],
    *,
    device: str = "cpu",
    batch_size: int = 16,
) -> np.ndarray:
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    model = load_local_sentence_model(model_path=model_path, device=device)
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    return np.asarray(vectors, dtype=np.float32)


@lru_cache(maxsize=2)
def load_local_index(index_dir_text: str) -> tuple[dict[str, Any], np.memmap, np.memmap]:
    index_dir = _require_existing_dir(index_dir_text, "Local semantic index dir")
    manifest_path = index_dir / "manifest.json"
    if not manifest_path.exists():
        raise _semantic_error(f"Local semantic index manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row_count = int(manifest.get("row_count", 0))
    vector_dim = int(manifest.get("vector_dim", 0))
    if row_count <= 0 or vector_dim <= 0:
        raise _semantic_error(f"Invalid local semantic manifest: {manifest_path}")

    vector_file = index_dir / str(manifest.get("vector_file", "chapter_vectors.f32"))
    chapter_uid_file = index_dir / str(manifest.get("chapter_uid_file", "chapter_uids.i64"))
    if not vector_file.exists():
        raise _semantic_error(f"Local vector file not found: {vector_file}")
    if not chapter_uid_file.exists():
        raise _semantic_error(f"Local chapter uid file not found: {chapter_uid_file}")

    vectors = np.memmap(vector_file, dtype=np.float32, mode="r", shape=(row_count, vector_dim))
    chapter_uids = np.memmap(chapter_uid_file, dtype=np.int64, mode="r", shape=(row_count,))
    return manifest, vectors, chapter_uids


def search_local_index(
    *,
    index_dir: str,
    query_vector: np.ndarray,
    limit: int,
    score_threshold: float,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    manifest, vectors, chapter_uids = load_local_index(index_dir)
    if query_vector.ndim != 1:
        raise _semantic_error("Expected a single query vector for local semantic search.")
    if query_vector.shape[0] != int(manifest["vector_dim"]):
        raise _semantic_error(
            f"Local vector dim mismatch: query={query_vector.shape[0]} index={manifest['vector_dim']}"
        )

    scores = np.asarray(vectors @ query_vector, dtype=np.float32)
    if score_threshold > 0:
        candidate_idx = np.flatnonzero(scores >= score_threshold)
    else:
        candidate_idx = np.arange(scores.shape[0], dtype=np.int64)

    if candidate_idx.size == 0:
        return []

    effective_limit = min(limit, int(candidate_idx.size))
    candidate_scores = scores[candidate_idx]
    if effective_limit >= int(candidate_idx.size):
        order = np.argsort(candidate_scores)[::-1]
        sorted_idx = candidate_idx[order]
    else:
        top_positions = np.argpartition(candidate_scores, -effective_limit)[-effective_limit:]
        top_idx = candidate_idx[top_positions]
        order = np.argsort(scores[top_idx])[::-1]
        sorted_idx = top_idx[order]

    results: list[dict[str, Any]] = []
    for idx in sorted_idx:
        chapter_uid = int(chapter_uids[int(idx)])
        score = float(scores[int(idx)])
        results.append(
            {
                "id": chapter_uid,
                "score": score,
                "payload": {
                    "chapter_uid": chapter_uid,
                    "text_preview": "",
                    "start_offset": -1,
                    "end_offset": -1,
                },
            }
        )
    return results


def local_semantic_retrieve_candidates(
    *,
    db_path: str,
    query_text: str,
    config: Any,
) -> dict[str, Any]:
    from service.semantic_retrieval import (
        aggregate_semantic_hits,
        build_semantic_candidate_rows,
        slice_query_semantic_chunks,
    )

    if not query_text.strip():
        raise ValueError("query_text is empty")

    index_dir = config.local_index_dir or str(DEFAULT_LOCAL_INDEX_DIR)
    model_path = config.local_model_path or resolve_default_local_model_path()
    query_chunks = slice_query_semantic_chunks(
        query_text=query_text,
        window_size=config.query_window_size,
        overlap_size=config.query_overlap_size,
    )
    query_texts = [query_text] + [str(item["text"]) for item in query_chunks]
    query_vectors = embed_texts_local(
        model_path=model_path,
        texts=query_texts,
        device=config.local_device,
        batch_size=config.local_encode_batch_size,
    )

    chapter_query_vector = query_vectors[0]
    chapter_hits = search_local_index(
        index_dir=index_dir,
        query_vector=chapter_query_vector,
        limit=config.chapter_top_k,
        score_threshold=config.score_threshold,
    )

    chunk_hits: list[dict[str, Any]] = []
    for query_chunk, vector in zip(query_chunks, query_vectors[1:]):
        hits = search_local_index(
            index_dir=index_dir,
            query_vector=np.asarray(vector, dtype=np.float32),
            limit=config.chunk_top_k,
            score_threshold=config.score_threshold,
        )
        for item in hits:
            row = dict(item)
            row["payload"] = dict(item.get("payload") or {})
            row["payload"]["text_preview"] = clip_text(str(query_chunk["text"]))
            row["query_chunk_order"] = int(query_chunk["chunk_order"])
            chunk_hits.append(row)

    aggregated_hits = aggregate_semantic_hits(
        chapter_hits=chapter_hits,
        chunk_hits=chunk_hits,
    )
    results = build_semantic_candidate_rows(
        db_path=db_path,
        aggregated_hits=aggregated_hits,
        merged_top_k=config.merged_top_k,
        source_name=f"local:{Path(index_dir).name}",
    )
    return {
        "status": "ok",
        "query_chunk_count": len(query_chunks),
        "chapter_hit_count": len(chapter_hits),
        "chunk_hit_count": len(chunk_hits),
        "candidate_count": len(results),
        "results": results,
    }
