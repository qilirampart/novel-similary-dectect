from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from statistics import median

from retrieve_candidates_v1 import retrieve_candidates
from v2_common import connect_db


def percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def safe_int(value: str | int) -> int:
    return int(value)


def summarize_scores(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "min": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": median(ordered),
        "p95": percentile(ordered, 0.95),
        "max": ordered[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--table", default="chapter_fts_v2")
    parser.add_argument(
        "--queries-csv",
        default="data_samples/retrieval_eval/self_short_eval_queries_v1.csv",
    )
    parser.add_argument("--candidate-limit", type=int, default=200)
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
        "--top-nonself",
        type=int,
        default=5,
        help="How many non-self candidates to export for each query",
    )
    parser.add_argument(
        "--detail-csv-out",
        default="data_samples/retrieval_eval/self_short_negative_candidates_v1.csv",
    )
    parser.add_argument(
        "--query-csv-out",
        default="data_samples/retrieval_eval/self_short_negative_query_summary_v1.csv",
    )
    parser.add_argument(
        "--json-out",
        default="data_samples/retrieval_eval/self_short_negative_summary_v1.json",
    )
    args = parser.parse_args()

    if args.candidate_limit <= 0:
        raise ValueError("--candidate-limit must be > 0")
    if args.top_nonself <= 0:
        raise ValueError("--top-nonself must be > 0")

    queries_path = Path(args.queries_csv)
    with queries_path.open("r", encoding="utf-8-sig", newline="") as f:
        queries = list(csv.DictReader(f))
    if not queries:
        raise ValueError("queries csv is empty")

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        detail_rows: list[dict[str, object]] = []
        query_rows: list[dict[str, object]] = []
        first_nonself_same_book = 0
        first_nonself_cross_book = 0
        first_nonself_same_chapter_name = 0
        first_nonself_same_chapter_ext_id = 0
        queries_with_self = 0
        queries_with_nonself = 0
        queries_nonself_beat_self = 0
        first_nonself_rank_counter: Counter[str] = Counter()
        first_nonself_scores: list[float] = []
        first_nonself_score_margins: list[float] = []
        top3_same_book = 0
        top3_cross_book = 0
        top5_same_book = 0
        top5_cross_book = 0
        nonself_rank1_query_ids: list[str] = []

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
            query_chapter_uid = safe_int(row["chapter_uid"])
            target_chapter_uid = safe_int(row.get(args.target_column) or row["chapter_uid"])
            query_book_ext_id = str(row["book_ext_id"])
            query_chapter_ext_id = str(row["chapter_ext_id"])
            query_chapter_name = str(row["chapter_name"])

            self_rank: int | None = None
            self_score: float | None = None
            first_nonself: dict[str, object] | None = None
            nonself_count = 0

            for overall_rank, item in enumerate(results, start=1):
                candidate_uid = int(item["chapter_uid"])
                is_self = candidate_uid == target_chapter_uid
                if is_self and self_rank is None:
                    self_rank = overall_rank
                    self_score = float(item["final_score"])
                    continue
                if is_self:
                    continue

                nonself_count += 1
                is_same_book = str(item["book_ext_id"]) == query_book_ext_id
                is_same_chapter_ext_id = str(item["chapter_ext_id"]) == query_chapter_ext_id
                is_same_chapter_name = str(item["chapter_name"]) == query_chapter_name

                if nonself_count <= args.top_nonself:
                    detail_rows.append(
                        {
                            "query_id": row["query_id"],
                            "dataset_key": row["dataset_key"],
                            "eval_mode": row.get("eval_mode", ""),
                            "query_chapter_uid": row["chapter_uid"],
                            "target_chapter_uid": target_chapter_uid,
                            "query_book_ext_id": row["book_ext_id"],
                            "query_book_name": row["book_name"],
                            "query_chapter_ext_id": row["chapter_ext_id"],
                            "query_chapter_name": row["chapter_name"],
                            "query_start": row["query_start"],
                            "query_len": row["query_len"],
                            "overall_rank": overall_rank,
                            "nonself_rank": nonself_count,
                            "candidate_chapter_uid": item["chapter_uid"],
                            "candidate_book_ext_id": item["book_ext_id"],
                            "candidate_book_name": item["book_name"],
                            "candidate_chapter_ext_id": item["chapter_ext_id"],
                            "candidate_chapter_name": item["chapter_name"],
                            "is_same_book": 1 if is_same_book else 0,
                            "is_same_chapter_ext_id": 1 if is_same_chapter_ext_id else 0,
                            "is_same_chapter_name": 1 if is_same_chapter_name else 0,
                            "final_score": f"{float(item['final_score']):.6f}",
                            "ngram_score": f"{float(item['ngram_score']):.6f}",
                            "coarse_score": f"{float(item['coarse_score']):.6f}",
                            "seed_hit_count": item["seed_hit_count"],
                            "seed_hit_weight": f"{float(item['seed_hit_weight']):.8f}",
                        }
                    )

                if first_nonself is None:
                    first_nonself = {
                        "overall_rank": overall_rank,
                        "nonself_rank": nonself_count,
                        "candidate_chapter_uid": item["chapter_uid"],
                        "candidate_book_ext_id": item["book_ext_id"],
                        "candidate_book_name": item["book_name"],
                        "candidate_chapter_ext_id": item["chapter_ext_id"],
                        "candidate_chapter_name": item["chapter_name"],
                        "is_same_book": is_same_book,
                        "is_same_chapter_ext_id": is_same_chapter_ext_id,
                        "is_same_chapter_name": is_same_chapter_name,
                        "final_score": float(item["final_score"]),
                        "ngram_score": float(item["ngram_score"]),
                        "coarse_score": float(item["coarse_score"]),
                    }

                if nonself_count <= 3:
                    if is_same_book:
                        top3_same_book += 1
                    else:
                        top3_cross_book += 1

                if nonself_count <= 5:
                    if is_same_book:
                        top5_same_book += 1
                    else:
                        top5_cross_book += 1

            if self_rank is not None:
                queries_with_self += 1

            if first_nonself is not None:
                queries_with_nonself += 1
                if bool(first_nonself["is_same_book"]):
                    first_nonself_same_book += 1
                else:
                    first_nonself_cross_book += 1
                if bool(first_nonself["is_same_chapter_name"]):
                    first_nonself_same_chapter_name += 1
                if bool(first_nonself["is_same_chapter_ext_id"]):
                    first_nonself_same_chapter_ext_id += 1
                if int(first_nonself["overall_rank"]) == 1:
                    nonself_rank1_query_ids.append(str(row["query_id"]))
                first_nonself_rank_counter[str(first_nonself["overall_rank"])] += 1
                first_nonself_scores.append(float(first_nonself["final_score"]))
                if self_score is not None:
                    first_nonself_score_margins.append(
                        float(first_nonself["final_score"]) - self_score
                    )
                    if int(first_nonself["overall_rank"]) < self_rank:
                        queries_nonself_beat_self += 1

            query_rows.append(
                {
                    "query_id": row["query_id"],
                    "dataset_key": row["dataset_key"],
                    "eval_mode": row.get("eval_mode", ""),
                    "query_chapter_uid": row["chapter_uid"],
                    "target_chapter_uid": target_chapter_uid,
                    "query_book_ext_id": row["book_ext_id"],
                    "query_book_name": row["book_name"],
                    "query_chapter_ext_id": row["chapter_ext_id"],
                    "query_chapter_name": row["chapter_name"],
                    "query_start": row["query_start"],
                    "query_len": row["query_len"],
                    "candidate_count": payload["candidate_count"],
                    "self_rank": self_rank if self_rank is not None else "",
                    "self_score": f"{self_score:.6f}" if self_score is not None else "",
                    "first_nonself_overall_rank": (
                        first_nonself["overall_rank"] if first_nonself is not None else ""
                    ),
                    "first_nonself_chapter_uid": (
                        first_nonself["candidate_chapter_uid"] if first_nonself is not None else ""
                    ),
                    "first_nonself_book_ext_id": (
                        first_nonself["candidate_book_ext_id"] if first_nonself is not None else ""
                    ),
                    "first_nonself_book_name": (
                        first_nonself["candidate_book_name"] if first_nonself is not None else ""
                    ),
                    "first_nonself_chapter_ext_id": (
                        first_nonself["candidate_chapter_ext_id"] if first_nonself is not None else ""
                    ),
                    "first_nonself_chapter_name": (
                        first_nonself["candidate_chapter_name"] if first_nonself is not None else ""
                    ),
                    "first_nonself_is_same_book": (
                        1 if first_nonself is not None and first_nonself["is_same_book"] else 0
                    ),
                    "first_nonself_is_same_chapter_ext_id": (
                        1
                        if first_nonself is not None
                        and first_nonself["is_same_chapter_ext_id"]
                        else 0
                    ),
                    "first_nonself_is_same_chapter_name": (
                        1 if first_nonself is not None and first_nonself["is_same_chapter_name"] else 0
                    ),
                    "first_nonself_final_score": (
                        f"{float(first_nonself['final_score']):.6f}"
                        if first_nonself is not None
                        else ""
                    ),
                    "first_nonself_ngram_score": (
                        f"{float(first_nonself['ngram_score']):.6f}"
                        if first_nonself is not None
                        else ""
                    ),
                    "first_nonself_coarse_score": (
                        f"{float(first_nonself['coarse_score']):.6f}"
                        if first_nonself is not None
                        else ""
                    ),
                    "first_nonself_minus_self_score": (
                        f"{(float(first_nonself['final_score']) - self_score):.6f}"
                        if first_nonself is not None and self_score is not None
                        else ""
                    ),
                }
            )
    finally:
        conn.close()

    total_queries = len(query_rows)
    summary = {
        "queries_csv": str(queries_path),
        "eval_mode": queries[0].get("eval_mode", "") if queries else "",
        "total_queries": total_queries,
        "queries_with_self": queries_with_self,
        "queries_with_nonself": queries_with_nonself,
        "queries_nonself_beat_self": queries_nonself_beat_self,
        "first_nonself_same_book_count": first_nonself_same_book,
        "first_nonself_cross_book_count": first_nonself_cross_book,
        "first_nonself_same_book_rate": (
            first_nonself_same_book / queries_with_nonself if queries_with_nonself else 0.0
        ),
        "first_nonself_cross_book_rate": (
            first_nonself_cross_book / queries_with_nonself if queries_with_nonself else 0.0
        ),
        "first_nonself_same_chapter_name_count": first_nonself_same_chapter_name,
        "first_nonself_same_chapter_ext_id_count": first_nonself_same_chapter_ext_id,
        "first_nonself_overall_rank_distribution": dict(first_nonself_rank_counter),
        "top3_nonself_same_book_count": top3_same_book,
        "top3_nonself_cross_book_count": top3_cross_book,
        "top5_nonself_same_book_count": top5_same_book,
        "top5_nonself_cross_book_count": top5_cross_book,
        "first_nonself_score_stats": summarize_scores(first_nonself_scores),
        "first_nonself_minus_self_score_stats": summarize_scores(first_nonself_score_margins),
        "nonself_rank1_query_ids": nonself_rank1_query_ids,
    }

    detail_csv_path = Path(args.detail_csv_out)
    detail_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with detail_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query_id",
                "dataset_key",
                "eval_mode",
                "query_chapter_uid",
                "target_chapter_uid",
                "query_book_ext_id",
                "query_book_name",
                "query_chapter_ext_id",
                "query_chapter_name",
                "query_start",
                "query_len",
                "overall_rank",
                "nonself_rank",
                "candidate_chapter_uid",
                "candidate_book_ext_id",
                "candidate_book_name",
                "candidate_chapter_ext_id",
                "candidate_chapter_name",
                "is_same_book",
                "is_same_chapter_ext_id",
                "is_same_chapter_name",
                "final_score",
                "ngram_score",
                "coarse_score",
                "seed_hit_count",
                "seed_hit_weight",
            ],
        )
        writer.writeheader()
        writer.writerows(detail_rows)

    query_csv_path = Path(args.query_csv_out)
    query_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with query_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query_id",
                "dataset_key",
                "eval_mode",
                "query_chapter_uid",
                "target_chapter_uid",
                "query_book_ext_id",
                "query_book_name",
                "query_chapter_ext_id",
                "query_chapter_name",
                "query_start",
                "query_len",
                "candidate_count",
                "self_rank",
                "self_score",
                "first_nonself_overall_rank",
                "first_nonself_chapter_uid",
                "first_nonself_book_ext_id",
                "first_nonself_book_name",
                "first_nonself_chapter_ext_id",
                "first_nonself_chapter_name",
                "first_nonself_is_same_book",
                "first_nonself_is_same_chapter_ext_id",
                "first_nonself_is_same_chapter_name",
                "first_nonself_final_score",
                "first_nonself_ngram_score",
                "first_nonself_coarse_score",
                "first_nonself_minus_self_score",
            ],
        )
        writer.writeheader()
        writer.writerows(query_rows)

    json_out_path = Path(args.json_out)
    json_out_path.parent.mkdir(parents=True, exist_ok=True)
    json_out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK total_queries={total_queries}")
    print(f"OK queries_with_self={queries_with_self}")
    print(f"OK queries_with_nonself={queries_with_nonself}")
    print(f"OK queries_nonself_beat_self={queries_nonself_beat_self}")
    print(f"OK first_nonself_same_book_rate={summary['first_nonself_same_book_rate']:.6f}")
    print(f"OK first_nonself_cross_book_rate={summary['first_nonself_cross_book_rate']:.6f}")
    print(f"OK detail_csv_out={detail_csv_path}")
    print(f"OK query_csv_out={query_csv_path}")
    print(f"OK json_out={json_out_path}")


if __name__ == "__main__":
    main()
