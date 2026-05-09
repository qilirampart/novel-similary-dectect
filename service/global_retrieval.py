from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from retrieve_candidates_v1 import retrieve_candidates  # noqa: E402
from v2_common import connect_db  # noqa: E402


DEFAULT_INDEXES: list[dict[str, str]] = [
    {
        "dataset_key": "self_short_novels",
        "table_name": "chapter_fts_v2_canonical",
    },
    {
        "dataset_key": "online_owned_novels",
        "table_name": "chapter_fts_online_owned_canonical_v1",
    },
]


@dataclass(frozen=True)
class RetrievalIndexTarget:
    dataset_key: str
    table_name: str


def load_default_targets() -> list[RetrievalIndexTarget]:
    return [RetrievalIndexTarget(**item) for item in DEFAULT_INDEXES]


def _retrieve_one_index(
    db_path: str,
    target: RetrievalIndexTarget,
    query_text: str,
    candidate_limit: int,
    ngram_size: int,
    max_query_ngrams: int,
    weight_coarse: float,
    seed_term_count: int,
) -> dict[str, Any]:
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        payload = retrieve_candidates(
            conn=conn,
            table_name=target.table_name,
            query_text=query_text,
            dataset_key=target.dataset_key,
            candidate_limit=candidate_limit,
            ngram_size=ngram_size,
            max_query_ngrams=max_query_ngrams,
            weight_coarse=weight_coarse,
            seed_term_count=seed_term_count,
        )
    finally:
        conn.close()

    return {
        "dataset_key": target.dataset_key,
        "table_name": target.table_name,
        "payload": payload,
    }


def merge_ranked_results(
    index_results: list[dict[str, Any]],
    merged_top_k: int = 20,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for result in index_results:
        dataset_key = str(result["dataset_key"])
        table_name = str(result["table_name"])
        payload = dict(result["payload"])
        for item in payload.get("results", []):
            row = dict(item)
            row["source_dataset_key"] = dataset_key
            row["source_table_name"] = table_name
            merged.append(row)

    merged.sort(
        key=lambda item: (
            -float(item["final_score"]),
            -float(item["ngram_score"]),
            -int(item["seed_hit_count"]),
            -float(item["seed_hit_weight"]),
            str(item["dataset_key"]),
            int(item["chapter_uid"]),
        )
    )
    return merged[:merged_top_k]


def global_retrieve_candidates(
    db_path: str,
    query_text: str,
    targets: list[RetrievalIndexTarget] | None = None,
    candidate_limit_per_index: int = 200,
    merged_top_k: int = 20,
    ngram_size: int = 3,
    max_query_ngrams: int = 120,
    weight_coarse: float = 0.25,
    seed_term_count: int = 12,
    max_workers: int = 4,
) -> dict[str, Any]:
    if not query_text.strip():
        raise ValueError("query_text is empty")
    if candidate_limit_per_index <= 0:
        raise ValueError("candidate_limit_per_index must be > 0")
    if merged_top_k <= 0:
        raise ValueError("merged_top_k must be > 0")

    effective_targets = targets or load_default_targets()
    if not effective_targets:
        raise ValueError("no retrieval targets configured")

    futures = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(effective_targets))) as executor:
        for target in effective_targets:
            futures.append(
                executor.submit(
                    _retrieve_one_index,
                    db_path,
                    target,
                    query_text,
                    candidate_limit_per_index,
                    ngram_size,
                    max_query_ngrams,
                    weight_coarse,
                    seed_term_count,
                )
            )
        index_results = [future.result() for future in futures]

    merged_results = merge_ranked_results(index_results=index_results, merged_top_k=merged_top_k)
    return {
        "query_text": query_text,
        "target_count": len(effective_targets),
        "candidate_limit_per_index": candidate_limit_per_index,
        "merged_top_k": merged_top_k,
        "index_results": index_results,
        "results": merged_results,
    }
