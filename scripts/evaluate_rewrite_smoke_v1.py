from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


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


def load_queries(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"queries csv is empty: {csv_path}")
    return rows


def run_one_query(args: argparse.Namespace, row: dict[str, str], json_out: Path, csv_out: Path) -> dict[str, Any]:
    cmd = [
        "python",
        "scripts/run_global_compare_v1.py",
        "--detection-mode",
        "rewrite",
        "--db",
        args.db,
        "--query-text",
        row["query_text"],
        "--merged-top-k",
        str(args.merged_top_k),
        "--compare-top-k",
        str(args.compare_top_k),
        "--top-k",
        str(args.top_k),
        "--window-size",
        str(args.window_size),
        "--step-size",
        str(args.step_size),
        "--semantic-backend",
        args.semantic_backend,
        "--semantic-remote-embedding-backend",
        args.semantic_remote_embedding_backend,
        "--semantic-ollama-url",
        args.semantic_ollama_url,
        "--semantic-qdrant-url",
        args.semantic_qdrant_url,
        "--semantic-model",
        args.semantic_model,
        "--semantic-gitee-endpoint",
        args.semantic_gitee_endpoint,
        "--semantic-gitee-token",
        args.semantic_gitee_token,
        "--semantic-gitee-token-env",
        args.semantic_gitee_token_env,
        "--semantic-gitee-dimensions",
        str(args.semantic_gitee_dimensions),
        "--semantic-chapter-collection",
        args.semantic_chapter_collection,
        "--semantic-chunk-collection",
        args.semantic_chunk_collection,
        "--semantic-query-window-size",
        str(args.semantic_query_window_size),
        "--semantic-query-overlap-size",
        str(args.semantic_query_overlap_size),
        "--semantic-chapter-top-k",
        str(args.semantic_chapter_top_k),
        "--semantic-chunk-top-k",
        str(args.semantic_chunk_top_k),
        "--semantic-score-threshold",
        str(args.semantic_score_threshold),
        "--json-out",
        str(json_out),
        "--csv-out",
        str(csv_out),
    ]
    if args.semantic_backend == "local":
        if args.semantic_local_index_dir:
            cmd.extend(["--semantic-local-index-dir", args.semantic_local_index_dir])
        if args.semantic_local_model_path:
            cmd.extend(["--semantic-local-model-path", args.semantic_local_model_path])
        cmd.extend(["--semantic-local-device", args.semantic_local_device])
        cmd.extend(["--semantic-local-encode-batch-size", str(args.semantic_local_encode_batch_size)])

    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    return {
        "stdout": completed.stdout,
        "payload": payload,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--queries-csv",
        default="data_samples/retrieval_eval/self_short_eval_queries_uid500_q300_canonical_v1.csv",
    )
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--sample-limit", type=int, default=0)
    parser.add_argument("--merged-top-k", type=int, default=10)
    parser.add_argument("--compare-top-k", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--window-size", type=int, default=200)
    parser.add_argument("--step-size", type=int, default=50)
    parser.add_argument("--semantic-backend", default="remote", choices=["remote", "local"])
    parser.add_argument(
        "--semantic-remote-embedding-backend",
        default="gitee_api",
        choices=["ollama", "gitee_api"],
    )
    parser.add_argument("--semantic-ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--semantic-qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--semantic-model", default="Qwen3-Embedding-8B")
    parser.add_argument("--semantic-gitee-endpoint", default="https://ai.gitee.com/v1/embeddings")
    parser.add_argument("--semantic-gitee-token", default="")
    parser.add_argument("--semantic-gitee-token-env", default="GITEE_AI_TOKEN")
    parser.add_argument("--semantic-gitee-dimensions", type=int, default=1024)
    parser.add_argument("--semantic-chapter-collection", default="novel_chapter_embeddings_smoke_500")
    parser.add_argument("--semantic-chunk-collection", default="novel_semantic_chunk_embeddings_smoke_500")
    parser.add_argument("--semantic-query-window-size", type=int, default=800)
    parser.add_argument("--semantic-query-overlap-size", type=int, default=200)
    parser.add_argument("--semantic-chapter-top-k", type=int, default=8)
    parser.add_argument("--semantic-chunk-top-k", type=int, default=8)
    parser.add_argument("--semantic-score-threshold", type=float, default=0.0)
    parser.add_argument("--semantic-local-index-dir", default="")
    parser.add_argument("--semantic-local-model-path", default="")
    parser.add_argument("--semantic-local-device", default="cpu")
    parser.add_argument("--semantic-local-encode-batch-size", type=int, default=16)
    parser.add_argument(
        "--csv-out",
        default="data_samples/retrieval_eval/rewrite_smoke500_q300_eval_v1.csv",
    )
    parser.add_argument(
        "--json-out",
        default="data_samples/retrieval_eval/rewrite_smoke500_q300_summary_v1.json",
    )
    args = parser.parse_args()

    queries_path = Path(args.queries_csv)
    queries = load_queries(queries_path)
    if args.sample_limit > 0:
        queries = queries[: args.sample_limit]

    output_csv = Path(args.csv_out)
    output_json = Path(args.json_out)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = output_csv.parent / "_tmp_rewrite_smoke_v1"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    rows_out: list[dict[str, Any]] = []
    self_ranks: list[int] = []
    reciprocal_rank_sum = 0.0
    hit_at_1 = 0
    hit_at_3 = 0
    hit_at_5 = 0
    hit_at_10 = 0
    semantic_ready_count = 0
    fallback_count = 0
    semantic_candidate_counts: list[float] = []
    suspicious_counts: list[float] = []
    coarse_target_counts: list[float] = []
    compared_candidate_counts: list[float] = []
    exact_top1_count = 0
    strong_top1_count = 0
    missing_target_count = 0

    for idx, row in enumerate(queries, start=1):
        tmp_json = tmp_dir / f"query_{idx:04d}.json"
        tmp_csv = tmp_dir / f"query_{idx:04d}.csv"
        result = run_one_query(args=args, row=row, json_out=tmp_json, csv_out=tmp_csv)
        payload = result["payload"]

        target_uid = int(row.get("target_chapter_uid") or row["chapter_uid"])
        fine_results = list(payload["fine"]["results"])
        top_results = fine_results[: args.top_k]
        self_rank = None
        self_item = None
        for rank, item in enumerate(top_results, start=1):
            if int(item["chapter_uid"]) == target_uid:
                self_rank = rank
                self_item = item
                break
        if self_item is None:
            for rank, item in enumerate(fine_results, start=1):
                if int(item["chapter_uid"]) == target_uid:
                    self_rank = rank
                    self_item = item
                    break

        rewrite_detection = payload["rewrite_detection"]
        semantic_status = str(rewrite_detection["status"])
        semantic_candidate_count = int(rewrite_detection["semantic_candidate_count"])
        suspicious_result_count = int(rewrite_detection["suspicious_result_count"])
        coarse_target_count = int(payload["coarse"]["target_count"])
        compared_candidate_count = int(payload["fine"]["compared_candidate_count"])

        coarse_target_counts.append(float(coarse_target_count))
        compared_candidate_counts.append(float(compared_candidate_count))
        semantic_candidate_counts.append(float(semantic_candidate_count))
        suspicious_counts.append(float(suspicious_result_count))

        if semantic_status == "semantic_ready":
            semantic_ready_count += 1
        elif semantic_status == "fallback_lexical_only":
            fallback_count += 1

        if fine_results:
            top1 = fine_results[0]
            if bool(top1["best_match"]["exact_substring_hit"]):
                exact_top1_count += 1
            if str(top1["review_label"]) == "强证据":
                strong_top1_count += 1
        else:
            top1 = None

        if self_rank is None:
            missing_target_count += 1
        else:
            self_ranks.append(self_rank)
            reciprocal_rank_sum += 1.0 / self_rank
            if self_rank <= 1:
                hit_at_1 += 1
            if self_rank <= 3:
                hit_at_3 += 1
            if self_rank <= 5:
                hit_at_5 += 1
            if self_rank <= 10:
                hit_at_10 += 1

        rows_out.append(
            {
                "query_id": row["query_id"],
                "eval_mode": row.get("eval_mode", ""),
                "dataset_key": row["dataset_key"],
                "target_chapter_uid": target_uid,
                "book_name": row["book_name"],
                "chapter_name": row["chapter_name"],
                "query_len": row["query_len"],
                "semantic_status": semantic_status,
                "semantic_candidate_count": semantic_candidate_count,
                "suspicious_result_count": suspicious_result_count,
                "coarse_target_count": coarse_target_count,
                "fine_compared_candidate_count": compared_candidate_count,
                "self_rank": self_rank if self_rank is not None else "",
                "self_fine_score": f"{float(self_item['fine_score']):.6f}" if self_item else "",
                "self_review_label": str(self_item["review_label"]) if self_item else "",
                "top1_chapter_uid": top1["chapter_uid"] if top1 else "",
                "top1_fine_score": f"{float(top1['fine_score']):.6f}" if top1 else "",
                "top1_review_label": str(top1["review_label"]) if top1 else "",
                "top1_exact_substring_hit": int(bool(top1["best_match"]["exact_substring_hit"])) if top1 else "",
                "top1_book_name": top1["book_name"] if top1 else "",
                "top1_chapter_name": top1["chapter_name"] if top1 else "",
            }
        )

        print(
            f"PROGRESS {idx}/{len(queries)} "
            f"query_id={row['query_id']} "
            f"semantic_status={semantic_status} "
            f"self_rank={self_rank if self_rank is not None else 'MISS'}"
        )

    total_queries = len(rows_out)
    summary = {
        "queries_csv": str(queries_path),
        "total_queries": total_queries,
        "semantic_backend": args.semantic_backend,
        "semantic_remote_embedding_backend": args.semantic_remote_embedding_backend,
        "semantic_ollama_url": args.semantic_ollama_url,
        "semantic_qdrant_url": args.semantic_qdrant_url,
        "semantic_model": args.semantic_model,
        "semantic_gitee_endpoint": args.semantic_gitee_endpoint,
        "semantic_gitee_token_env": args.semantic_gitee_token_env,
        "semantic_gitee_dimensions": args.semantic_gitee_dimensions,
        "semantic_chapter_collection": args.semantic_chapter_collection,
        "semantic_chunk_collection": args.semantic_chunk_collection,
        "semantic_query_window_size": args.semantic_query_window_size,
        "semantic_query_overlap_size": args.semantic_query_overlap_size,
        "semantic_chapter_top_k": args.semantic_chapter_top_k,
        "semantic_chunk_top_k": args.semantic_chunk_top_k,
        "merged_top_k": args.merged_top_k,
        "compare_top_k": args.compare_top_k,
        "top_k": args.top_k,
        "window_size": args.window_size,
        "step_size": args.step_size,
        "semantic_ready_count": semantic_ready_count,
        "fallback_lexical_only_count": fallback_count,
        "hit_at_1": hit_at_1 / total_queries if total_queries else 0.0,
        "hit_at_3": hit_at_3 / total_queries if total_queries else 0.0,
        "hit_at_5": hit_at_5 / total_queries if total_queries else 0.0,
        "hit_at_10": hit_at_10 / total_queries if total_queries else 0.0,
        "mrr": reciprocal_rank_sum / total_queries if total_queries else 0.0,
        "median_self_rank": median(sorted(self_ranks)) if self_ranks else None,
        "p95_self_rank": percentile([float(v) for v in sorted(self_ranks)], 0.95) if self_ranks else None,
        "missing_target_count": missing_target_count,
        "top1_exact_substring_ratio": exact_top1_count / total_queries if total_queries else 0.0,
        "top1_strong_evidence_ratio": strong_top1_count / total_queries if total_queries else 0.0,
        "semantic_candidate_count_stats": build_summary_stats(semantic_candidate_counts),
        "suspicious_result_count_stats": build_summary_stats(suspicious_counts),
        "coarse_target_count_stats": build_summary_stats(coarse_target_counts),
        "fine_compared_candidate_count_stats": build_summary_stats(compared_candidate_counts),
    }

    with output_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query_id",
                "eval_mode",
                "dataset_key",
                "target_chapter_uid",
                "book_name",
                "chapter_name",
                "query_len",
                "semantic_status",
                "semantic_candidate_count",
                "suspicious_result_count",
                "coarse_target_count",
                "fine_compared_candidate_count",
                "self_rank",
                "self_fine_score",
                "self_review_label",
                "top1_chapter_uid",
                "top1_fine_score",
                "top1_review_label",
                "top1_exact_substring_hit",
                "top1_book_name",
                "top1_chapter_name",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK total_queries={total_queries}")
    print(f"OK semantic_ready_count={semantic_ready_count}")
    print(f"OK fallback_lexical_only_count={fallback_count}")
    print(f"OK hit_at_1={summary['hit_at_1']:.6f}")
    print(f"OK hit_at_3={summary['hit_at_3']:.6f}")
    print(f"OK hit_at_5={summary['hit_at_5']:.6f}")
    print(f"OK hit_at_10={summary['hit_at_10']:.6f}")
    print(f"OK mrr={summary['mrr']:.6f}")
    print(f"OK missing_target_count={missing_target_count}")
    print(f"OK csv_out={output_csv}")
    print(f"OK json_out={output_json}")


if __name__ == "__main__":
    main()
