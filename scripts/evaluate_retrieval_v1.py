from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import median

from retrieve_candidates_v1 import retrieve_candidates
from v2_common import connect_db


def percentile(sorted_values: list[int], ratio: float) -> int | None:
    if not sorted_values:
        return None
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--table", default="chapter_fts_v2")
    parser.add_argument(
        "--queries-csv",
        default="data_samples/retrieval_eval/self_short_eval_queries_v1.csv",
    )
    parser.add_argument("--candidate-limit", type=int, default=200)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--ngram-size", type=int, default=3)
    parser.add_argument("--max-query-ngrams", type=int, default=120)
    parser.add_argument("--weight-coarse", type=float, default=0.25)
    parser.add_argument("--seed-term-count", type=int, default=12)
    parser.add_argument(
        "--target-column",
        default="chapter_uid",
        help="Which CSV column should be treated as the retrieval target chapter uid",
    )
    parser.add_argument(
        "--csv-out",
        default="data_samples/retrieval_eval/self_short_eval_results_v1.csv",
    )
    parser.add_argument(
        "--json-out",
        default="data_samples/retrieval_eval/self_short_eval_summary_v1.json",
    )
    args = parser.parse_args()

    if args.candidate_limit <= 0:
        raise ValueError("--candidate-limit must be > 0")
    if args.top_k <= 0:
        raise ValueError("--top-k must be > 0")

    queries_path = Path(args.queries_csv)
    with queries_path.open("r", encoding="utf-8-sig", newline="") as f:
        queries = list(csv.DictReader(f))
    if not queries:
        raise ValueError("queries csv is empty")

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        rows_out: list[dict[str, object]] = []
        self_ranks: list[int] = []
        hit_at_1 = 0
        hit_at_5 = 0
        hit_at_10 = 0
        reciprocal_rank_sum = 0.0

        for row in queries:
            payload = retrieve_candidates(
                conn=conn,
                table_name=args.table,
                query_text=row["query_text"],
                dataset_key=row["dataset_key"],
                candidate_limit=args.candidate_limit,
                ngram_size=args.ngram_size,
                max_query_ngrams=args.max_query_ngrams,
                weight_coarse=args.weight_coarse,
                seed_term_count=args.seed_term_count,
            )
            results = list(payload["results"])
            query_chapter_uid = int(row["chapter_uid"])
            target_uid = int(row.get(args.target_column) or row["chapter_uid"])
            self_rank = None
            for idx, item in enumerate(results, start=1):
                if int(item["chapter_uid"]) == target_uid:
                    self_rank = idx
                    break

            top1 = results[0] if results else None
            if self_rank is not None:
                self_ranks.append(self_rank)
                reciprocal_rank_sum += 1.0 / self_rank
                if self_rank <= 1:
                    hit_at_1 += 1
                if self_rank <= 5:
                    hit_at_5 += 1
                if self_rank <= 10:
                    hit_at_10 += 1

            rows_out.append(
                {
                    "query_id": row["query_id"],
                    "dataset_key": row["dataset_key"],
                    "eval_mode": row.get("eval_mode", ""),
                    "chapter_uid": query_chapter_uid,
                    "target_chapter_uid": target_uid,
                    "book_ext_id": row["book_ext_id"],
                    "book_name": row["book_name"],
                    "chapter_ext_id": row["chapter_ext_id"],
                    "chapter_name": row["chapter_name"],
                    "query_start": row["query_start"],
                    "query_len": row["query_len"],
                    "candidate_count": payload["candidate_count"],
                    "query_ngrams": payload["query_ngrams"],
                    "self_rank": self_rank if self_rank is not None else "",
                    "top1_chapter_uid": top1["chapter_uid"] if top1 else "",
                    "top1_book_ext_id": top1["book_ext_id"] if top1 else "",
                    "top1_chapter_ext_id": top1["chapter_ext_id"] if top1 else "",
                    "top1_chapter_name": top1["chapter_name"] if top1 else "",
                    "top1_final_score": f"{top1['final_score']:.6f}" if top1 else "",
                }
            )
    finally:
        conn.close()

    total_queries = len(rows_out)
    found_queries = len(self_ranks)
    sorted_ranks = sorted(self_ranks)
    summary = {
        "queries_csv": str(queries_path),
        "eval_mode": queries[0].get("eval_mode", "") if queries else "",
        "total_queries": total_queries,
        "found_queries": found_queries,
        "candidate_limit": args.candidate_limit,
        "top_k": args.top_k,
        "hit_at_1": hit_at_1 / total_queries if total_queries else 0.0,
        "hit_at_5": hit_at_5 / total_queries if total_queries else 0.0,
        "hit_at_10": hit_at_10 / total_queries if total_queries else 0.0,
        "mrr": reciprocal_rank_sum / total_queries if total_queries else 0.0,
        "median_self_rank": median(sorted_ranks) if sorted_ranks else None,
        "p95_self_rank": percentile(sorted_ranks, 0.95),
        "missing_self_count": total_queries - found_queries,
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
                "candidate_count",
                "query_ngrams",
                "self_rank",
                "top1_chapter_uid",
                "top1_book_ext_id",
                "top1_chapter_ext_id",
                "top1_chapter_name",
                "top1_final_score",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    json_out_path = Path(args.json_out)
    json_out_path.parent.mkdir(parents=True, exist_ok=True)
    json_out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK total_queries={summary['total_queries']}")
    print(f"OK found_queries={summary['found_queries']}")
    print(f"OK hit_at_1={summary['hit_at_1']:.6f}")
    print(f"OK hit_at_5={summary['hit_at_5']:.6f}")
    print(f"OK hit_at_10={summary['hit_at_10']:.6f}")
    print(f"OK mrr={summary['mrr']:.6f}")
    print(f"OK median_self_rank={summary['median_self_rank']}")
    print(f"OK p95_self_rank={summary['p95_self_rank']}")
    print(f"OK csv_out={csv_out_path}")
    print(f"OK json_out={json_out_path}")


if __name__ == "__main__":
    main()
