from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import median
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.semantic_retrieval import (
    DEFAULT_CHAPTER_COLLECTION,
    DEFAULT_CHUNK_COLLECTION,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_QDRANT_URL,
    DEFAULT_REMOTE_EMBEDDING_BACKEND,
    SemanticRetrievalConfig,
    semantic_retrieve_candidates,
)


def percentile(sorted_values: list[int], ratio: float) -> int | None:
    if not sorted_values:
        return None
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument(
        "--queries-csv",
        default="data_samples/retrieval_eval/self_short_eval_queries_canonical_sample10_v1.csv",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--remote-embedding-backend",
        default=DEFAULT_REMOTE_EMBEDDING_BACKEND,
        choices=["ollama", "gitee_api"],
    )
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--gitee-endpoint", default="https://ai.gitee.com/v1/embeddings")
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--gitee-token-env", default="GITEE_AI_TOKEN")
    parser.add_argument("--gitee-dimensions", type=int, default=1024)
    parser.add_argument("--chapter-collection", default=DEFAULT_CHAPTER_COLLECTION)
    parser.add_argument("--chunk-collection", default=DEFAULT_CHUNK_COLLECTION)
    parser.add_argument("--query-window-size", type=int, default=800)
    parser.add_argument("--query-overlap-size", type=int, default=200)
    parser.add_argument("--chapter-top-k", type=int, default=0)
    parser.add_argument("--chunk-top-k", type=int, default=12)
    parser.add_argument("--merged-top-k", type=int, default=10)
    parser.add_argument("--score-threshold", type=float, default=0.0)
    parser.add_argument(
        "--target-column",
        default="target_chapter_uid",
        help="Which CSV column should be treated as the semantic retrieval target chapter uid",
    )
    parser.add_argument(
        "--max-target-chapter-uid",
        type=int,
        default=0,
        help="If > 0, only evaluate rows whose target chapter uid is <= this value",
    )
    parser.add_argument(
        "--csv-out",
        default="data_samples/retrieval_eval/self_short_semantic_recall_eval_sample10_v1.csv",
    )
    parser.add_argument(
        "--json-out",
        default="data_samples/retrieval_eval/self_short_semantic_recall_summary_sample10_v1.json",
    )
    args = parser.parse_args()

    queries_path = Path(args.queries_csv)
    with queries_path.open("r", encoding="utf-8-sig", newline="") as f:
        queries = list(csv.DictReader(f))
    if args.max_target_chapter_uid > 0:
        queries = [
            row
            for row in queries
            if int(row.get(args.target_column) or row["chapter_uid"]) <= args.max_target_chapter_uid
        ]
    if not queries:
        raise ValueError("queries csv is empty")

    config = SemanticRetrievalConfig(
        remote_embedding_backend=args.remote_embedding_backend,
        ollama_url=args.ollama_url,
        qdrant_url=args.qdrant_url,
        model=args.model,
        gitee_endpoint=args.gitee_endpoint,
        gitee_token=args.gitee_token,
        gitee_token_env=args.gitee_token_env,
        gitee_dimensions=args.gitee_dimensions,
        chapter_collection=args.chapter_collection,
        chunk_collection=args.chunk_collection,
        query_window_size=args.query_window_size,
        query_overlap_size=args.query_overlap_size,
        chapter_top_k=args.chapter_top_k,
        chunk_top_k=args.chunk_top_k,
        merged_top_k=args.merged_top_k,
        score_threshold=args.score_threshold,
    )

    rows_out: list[dict[str, object]] = []
    target_ranks: list[int] = []
    hit_at_1 = 0
    hit_at_3 = 0
    hit_at_5 = 0
    hit_at_10 = 0
    reciprocal_rank_sum = 0.0

    for row in queries:
        target_uid = int(row.get(args.target_column) or row["chapter_uid"])
        payload = semantic_retrieve_candidates(
            db_path=args.db,
            query_text=row["query_text"],
            config=config,
        )
        results = list(payload["results"])
        target_rank = None
        for idx, item in enumerate(results, start=1):
            if int(item["chapter_uid"]) == target_uid:
                target_rank = idx
                break

        top1 = results[0] if results else None
        if target_rank is not None:
            target_ranks.append(target_rank)
            reciprocal_rank_sum += 1.0 / target_rank
            if target_rank <= 1:
                hit_at_1 += 1
            if target_rank <= 3:
                hit_at_3 += 1
            if target_rank <= 5:
                hit_at_5 += 1
            if target_rank <= 10:
                hit_at_10 += 1

        rows_out.append(
            {
                "query_id": row["query_id"],
                "dataset_key": row["dataset_key"],
                "eval_mode": row.get("eval_mode", ""),
                "chapter_uid": row["chapter_uid"],
                "target_chapter_uid": target_uid,
                "book_ext_id": row["book_ext_id"],
                "book_name": row["book_name"],
                "chapter_ext_id": row["chapter_ext_id"],
                "chapter_name": row["chapter_name"],
                "query_start": row["query_start"],
                "query_len": row["query_len"],
                "semantic_query_chunk_count": payload["query_chunk_count"],
                "semantic_candidate_count": payload["candidate_count"],
                "target_rank": target_rank if target_rank is not None else "",
                "top1_chapter_uid": top1["chapter_uid"] if top1 else "",
                "top1_book_ext_id": top1["book_ext_id"] if top1 else "",
                "top1_chapter_ext_id": top1["chapter_ext_id"] if top1 else "",
                "top1_chapter_name": top1["chapter_name"] if top1 else "",
                "top1_final_score": f"{top1['final_score']:.6f}" if top1 else "",
                "top1_semantic_chunk_score_max": (
                    f"{top1['semantic_chunk_score_max']:.6f}" if top1 else ""
                ),
                "top1_semantic_chapter_score_max": (
                    f"{top1['semantic_chapter_score_max']:.6f}" if top1 else ""
                ),
            }
        )

    total_queries = len(rows_out)
    found_queries = len(target_ranks)
    sorted_ranks = sorted(target_ranks)
    summary = {
        "queries_csv": str(queries_path),
        "eval_mode": queries[0].get("eval_mode", "") if queries else "",
        "total_queries": total_queries,
        "found_queries": found_queries,
        "merged_top_k": args.merged_top_k,
        "chapter_top_k": args.chapter_top_k,
        "chunk_top_k": args.chunk_top_k,
        "hit_at_1": hit_at_1 / total_queries if total_queries else 0.0,
        "hit_at_3": hit_at_3 / total_queries if total_queries else 0.0,
        "hit_at_5": hit_at_5 / total_queries if total_queries else 0.0,
        "hit_at_10": hit_at_10 / total_queries if total_queries else 0.0,
        "mrr": reciprocal_rank_sum / total_queries if total_queries else 0.0,
        "median_target_rank": median(sorted_ranks) if sorted_ranks else None,
        "p95_target_rank": percentile(sorted_ranks, 0.95),
        "missing_target_count": total_queries - found_queries,
    }

    csv_out_path = Path(args.csv_out)
    csv_out_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query_id",
                "dataset_key",
                "eval_mode",
                "chapter_uid",
                "target_chapter_uid",
                "book_ext_id",
                "book_name",
                "chapter_ext_id",
                "chapter_name",
                "query_start",
                "query_len",
                "semantic_query_chunk_count",
                "semantic_candidate_count",
                "target_rank",
                "top1_chapter_uid",
                "top1_book_ext_id",
                "top1_chapter_ext_id",
                "top1_chapter_name",
                "top1_final_score",
                "top1_semantic_chunk_score_max",
                "top1_semantic_chapter_score_max",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    json_out_path = Path(args.json_out)
    json_out_path.parent.mkdir(parents=True, exist_ok=True)
    json_out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK total_queries={summary['total_queries']}")
    print(f"OK found_queries={summary['found_queries']}")
    print(f"OK max_target_chapter_uid={args.max_target_chapter_uid}")
    print(f"OK hit_at_1={summary['hit_at_1']:.6f}")
    print(f"OK hit_at_3={summary['hit_at_3']:.6f}")
    print(f"OK hit_at_5={summary['hit_at_5']:.6f}")
    print(f"OK hit_at_10={summary['hit_at_10']:.6f}")
    print(f"OK mrr={summary['mrr']:.6f}")
    print(f"OK median_target_rank={summary['median_target_rank']}")
    print(f"OK p95_target_rank={summary['p95_target_rank']}")
    print(f"OK csv_out={csv_out_path}")
    print(f"OK json_out={json_out_path}")


if __name__ == "__main__":
    main()
