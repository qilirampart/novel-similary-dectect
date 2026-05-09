from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import median
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.candidate_slicing import slice_candidate_chapters
from service.fine_compare import compare_query_to_candidates
from service.global_retrieval import global_retrieve_candidates


def percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def safe_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def build_summary_stats(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    if not ordered:
        return {"min": None, "p50": None, "p95": None, "max": None}
    return {
        "min": ordered[0],
        "p50": percentile(ordered, 0.50),
        "p95": percentile(ordered, 0.95),
        "max": ordered[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument(
        "--queries-csv",
        default="data_samples/retrieval_eval/self_short_eval_queries_canonical_v1.csv",
    )
    parser.add_argument("--candidate-limit-per-index", type=int, default=200)
    parser.add_argument("--merged-top-k", type=int, default=20)
    parser.add_argument("--compare-top-k", type=int, default=10)
    parser.add_argument("--ngram-size", type=int, default=3)
    parser.add_argument("--max-query-ngrams", type=int, default=120)
    parser.add_argument("--weight-coarse", type=float, default=0.25)
    parser.add_argument("--seed-term-count", type=int, default=12)
    parser.add_argument("--window-size", type=int, default=200)
    parser.add_argument("--step-size", type=int, default=50)
    parser.add_argument(
        "--target-column",
        default="target_chapter_uid",
        help="Which CSV column should be treated as the compare target chapter uid",
    )
    parser.add_argument(
        "--csv-out",
        default="data_samples/retrieval_eval/self_short_global_compare_eval_v1.csv",
    )
    parser.add_argument(
        "--json-out",
        default="data_samples/retrieval_eval/self_short_global_compare_summary_v1.json",
    )
    args = parser.parse_args()

    queries_path = Path(args.queries_csv)
    with queries_path.open("r", encoding="utf-8-sig", newline="") as f:
        queries = list(csv.DictReader(f))
    if not queries:
        raise ValueError("queries csv is empty")

    rows_out: list[dict[str, Any]] = []
    self_ranks: list[int] = []
    self_scores: list[float] = []
    nonself_scores: list[float] = []
    top1_scores: list[float] = []
    confidence_counts = {"强证据": 0, "中证据": 0, "弱证据": 0}
    hit_at_1 = 0
    hit_at_3 = 0
    hit_at_5 = 0
    reciprocal_rank_sum = 0.0
    queries_with_self = 0
    queries_with_nonself = 0
    queries_nonself_beat_self = 0

    for row in queries:
        query_text = row["query_text"]
        target_uid = int(row.get(args.target_column) or row["chapter_uid"])

        coarse_payload = global_retrieve_candidates(
            db_path=args.db,
            query_text=query_text,
            candidate_limit_per_index=args.candidate_limit_per_index,
            merged_top_k=args.merged_top_k,
            ngram_size=args.ngram_size,
            max_query_ngrams=args.max_query_ngrams,
            weight_coarse=args.weight_coarse,
            seed_term_count=args.seed_term_count,
        )
        coarse_candidates = list(coarse_payload["results"])[: args.compare_top_k]
        chapter_uids = [int(item["chapter_uid"]) for item in coarse_candidates]
        sliced_candidates = slice_candidate_chapters(
            db_path=args.db,
            chapter_uids=chapter_uids,
            window_size=args.window_size,
            step_size=args.step_size,
        )
        fine_results = compare_query_to_candidates(
            query_text=query_text,
            candidates=coarse_candidates,
            sliced_candidates=sliced_candidates,
            window_size=args.window_size,
            step_size=args.step_size,
            ngram_size=args.ngram_size,
        )

        top1 = fine_results[0] if fine_results else None
        if top1 is not None:
            top1_scores.append(float(top1["fine_score"]))

        self_item = None
        self_rank = None
        for idx, item in enumerate(fine_results, start=1):
            if int(item["chapter_uid"]) == target_uid:
                self_item = item
                self_rank = idx
                break

        first_nonself = None
        for item in fine_results:
            if int(item["chapter_uid"]) != target_uid:
                first_nonself = item
                break

        if self_item is not None and self_rank is not None:
            queries_with_self += 1
            self_ranks.append(self_rank)
            self_score = float(self_item["fine_score"])
            self_scores.append(self_score)
            reciprocal_rank_sum += 1.0 / self_rank
            confidence_counts[str(self_item["review_label"])] += 1
            if self_rank <= 1:
                hit_at_1 += 1
            if self_rank <= 3:
                hit_at_3 += 1
            if self_rank <= 5:
                hit_at_5 += 1

        if first_nonself is not None:
            queries_with_nonself += 1
            nonself_score = float(first_nonself["fine_score"])
            nonself_scores.append(nonself_score)
            if self_item is not None and nonself_score >= float(self_item["fine_score"]):
                queries_nonself_beat_self += 1

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
                "coarse_candidate_count": len(coarse_payload["results"]),
                "fine_candidate_count": len(fine_results),
                "self_rank": self_rank if self_rank is not None else "",
                "self_fine_score": f"{float(self_item['fine_score']):.6f}" if self_item else "",
                "self_review_label": str(self_item["review_label"]) if self_item else "",
                "self_confidence_label": str(self_item["confidence_label"]) if self_item else "",
                "self_longest_match_ratio": (
                    f"{float(self_item['best_match']['longest_match_ratio']):.6f}" if self_item else ""
                ),
                "self_exact_substring_hit": int(bool(self_item["best_match"]["exact_substring_hit"])) if self_item else "",
                "top1_chapter_uid": top1["chapter_uid"] if top1 else "",
                "top1_fine_score": f"{float(top1['fine_score']):.6f}" if top1 else "",
                "top1_review_label": str(top1["review_label"]) if top1 else "",
                "top1_confidence_label": str(top1["confidence_label"]) if top1 else "",
                "first_nonself_chapter_uid": first_nonself["chapter_uid"] if first_nonself else "",
                "first_nonself_fine_score": f"{float(first_nonself['fine_score']):.6f}" if first_nonself else "",
                "first_nonself_review_label": str(first_nonself["review_label"]) if first_nonself else "",
                "first_nonself_longest_match_ratio": (
                    f"{float(first_nonself['best_match']['longest_match_ratio']):.6f}" if first_nonself else ""
                ),
            }
        )

    total_queries = len(rows_out)
    sorted_self_ranks = sorted(self_ranks)
    summary = {
        "queries_csv": str(queries_path),
        "eval_mode": queries[0].get("eval_mode", "") if queries else "",
        "total_queries": total_queries,
        "queries_with_self": queries_with_self,
        "queries_with_nonself": queries_with_nonself,
        "queries_nonself_beat_self": queries_nonself_beat_self,
        "candidate_limit_per_index": args.candidate_limit_per_index,
        "merged_top_k": args.merged_top_k,
        "compare_top_k": args.compare_top_k,
        "window_size": args.window_size,
        "step_size": args.step_size,
        "hit_at_1": hit_at_1 / total_queries if total_queries else 0.0,
        "hit_at_3": hit_at_3 / total_queries if total_queries else 0.0,
        "hit_at_5": hit_at_5 / total_queries if total_queries else 0.0,
        "mrr": reciprocal_rank_sum / total_queries if total_queries else 0.0,
        "median_self_rank": median(sorted_self_ranks) if sorted_self_ranks else None,
        "p95_self_rank": percentile([float(v) for v in sorted_self_ranks], 0.95),
        "self_review_label_distribution": confidence_counts,
        "self_score_stats": build_summary_stats(self_scores),
        "first_nonself_score_stats": build_summary_stats(nonself_scores),
        "top1_score_stats": build_summary_stats(top1_scores),
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
                "coarse_candidate_count",
                "fine_candidate_count",
                "self_rank",
                "self_fine_score",
                "self_review_label",
                "self_confidence_label",
                "self_longest_match_ratio",
                "self_exact_substring_hit",
                "top1_chapter_uid",
                "top1_fine_score",
                "top1_review_label",
                "top1_confidence_label",
                "first_nonself_chapter_uid",
                "first_nonself_fine_score",
                "first_nonself_review_label",
                "first_nonself_longest_match_ratio",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    json_out_path = Path(args.json_out)
    json_out_path.parent.mkdir(parents=True, exist_ok=True)
    json_out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK total_queries={summary['total_queries']}")
    print(f"OK queries_with_self={summary['queries_with_self']}")
    print(f"OK queries_with_nonself={summary['queries_with_nonself']}")
    print(f"OK queries_nonself_beat_self={summary['queries_nonself_beat_self']}")
    print(f"OK hit_at_1={summary['hit_at_1']:.6f}")
    print(f"OK hit_at_3={summary['hit_at_3']:.6f}")
    print(f"OK hit_at_5={summary['hit_at_5']:.6f}")
    print(f"OK mrr={summary['mrr']:.6f}")
    print(f"OK csv_out={csv_out_path}")
    print(f"OK json_out={json_out_path}")


if __name__ == "__main__":
    main()
