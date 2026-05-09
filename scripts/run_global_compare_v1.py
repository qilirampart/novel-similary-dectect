from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline
from service.detection_modes import list_detection_modes
from service.semantic_retrieval import (
    DEFAULT_CHAPTER_COLLECTION,
    DEFAULT_CHUNK_COLLECTION,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_QDRANT_URL,
    DEFAULT_REMOTE_EMBEDDING_BACKEND,
    SemanticRetrievalConfig,
)


def load_query_text(query_text: str, query_file: str) -> str:
    if query_text:
        return query_text
    if query_file:
        return Path(query_file).read_text(encoding="utf-8")
    raise ValueError("Either --query-text or --query-file is required")

def write_review_csv(csv_out: str, rows: list[dict[str, object]]) -> None:
    import csv

    if not csv_out:
        return
    path = Path(csv_out)
    fieldnames = [
        "detection_mode",
        "fine_rank",
        "review_label",
        "confidence_label",
        "fine_score",
        "coarse_rank",
        "coarse_final_score",
        "dataset_key",
        "book_ext_id",
        "book_name",
        "chapter_uid",
        "chapter_ext_id",
        "chapter_name",
        "candidate_window_order",
        "candidate_start_offset",
        "candidate_end_offset",
        "exact_substring_hit",
        "longest_match_len",
        "longest_match_ratio",
        "ngram_recall",
        "ngram_precision",
        "jaccard",
        "sequence_ratio",
        "matched_substring",
        "query_text_preview",
        "candidate_text_preview",
        "query_text",
        "candidate_text",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--detection-mode", default="reuse", choices=list_detection_modes())
    parser.add_argument("--query-text", default="")
    parser.add_argument("--query-file", default="")
    parser.add_argument("--candidate-limit-per-index", type=int, default=200)
    parser.add_argument("--merged-top-k", type=int)
    parser.add_argument("--compare-top-k", type=int)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--window-size", type=int, default=200)
    parser.add_argument("--step-size", type=int, default=50)
    parser.add_argument("--ngram-size", type=int, default=3)
    parser.add_argument("--max-query-ngrams", type=int, default=120)
    parser.add_argument("--weight-coarse", type=float, default=0.25)
    parser.add_argument("--seed-term-count", type=int, default=12)
    parser.add_argument("--semantic-backend", default="remote", choices=["remote", "local"])
    parser.add_argument(
        "--semantic-remote-embedding-backend",
        default=DEFAULT_REMOTE_EMBEDDING_BACKEND,
        choices=["ollama", "gitee_api"],
    )
    parser.add_argument("--semantic-ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--semantic-qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--semantic-model", default=DEFAULT_MODEL)
    parser.add_argument("--semantic-gitee-endpoint", default="https://ai.gitee.com/v1/embeddings")
    parser.add_argument("--semantic-gitee-token", default="")
    parser.add_argument("--semantic-gitee-token-env", default="GITEE_AI_TOKEN")
    parser.add_argument("--semantic-gitee-dimensions", type=int, default=1024)
    parser.add_argument("--semantic-chapter-collection", default=DEFAULT_CHAPTER_COLLECTION)
    parser.add_argument("--semantic-chunk-collection", default=DEFAULT_CHUNK_COLLECTION)
    parser.add_argument("--semantic-query-window-size", type=int, default=800)
    parser.add_argument("--semantic-query-overlap-size", type=int, default=200)
    parser.add_argument("--semantic-chapter-top-k", type=int, default=0)
    parser.add_argument("--semantic-chunk-top-k", type=int, default=12)
    parser.add_argument("--semantic-score-threshold", type=float, default=0.0)
    parser.add_argument("--semantic-local-index-dir", default="")
    parser.add_argument("--semantic-local-model-path", default="")
    parser.add_argument("--semantic-local-device", default="cpu")
    parser.add_argument("--semantic-local-encode-batch-size", type=int, default=16)
    parser.add_argument("--disable-semantic-recall", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--csv-out", default="")
    args = parser.parse_args()

    query_text = load_query_text(args.query_text, args.query_file)
    semantic_config = SemanticRetrievalConfig(
        backend=args.semantic_backend,
        remote_embedding_backend=args.semantic_remote_embedding_backend,
        ollama_url=args.semantic_ollama_url,
        qdrant_url=args.semantic_qdrant_url,
        model=args.semantic_model,
        gitee_endpoint=args.semantic_gitee_endpoint,
        gitee_token=args.semantic_gitee_token,
        gitee_token_env=args.semantic_gitee_token_env,
        gitee_dimensions=args.semantic_gitee_dimensions,
        chapter_collection=args.semantic_chapter_collection,
        chunk_collection=args.semantic_chunk_collection,
        query_window_size=args.semantic_query_window_size,
        query_overlap_size=args.semantic_query_overlap_size,
        chapter_top_k=args.semantic_chapter_top_k,
        chunk_top_k=args.semantic_chunk_top_k,
        merged_top_k=args.merged_top_k or None,
        score_threshold=args.semantic_score_threshold,
        local_index_dir=args.semantic_local_index_dir,
        local_model_path=args.semantic_local_model_path,
        local_device=args.semantic_local_device,
        local_encode_batch_size=args.semantic_local_encode_batch_size,
    )
    payload = run_compare_pipeline(
        ComparePipelineRequest(
            db_path=args.db,
            detection_mode=args.detection_mode,
            query_text=query_text,
            candidate_limit_per_index=args.candidate_limit_per_index,
            merged_top_k=args.merged_top_k,
            compare_top_k=args.compare_top_k,
            top_k=args.top_k,
            window_size=args.window_size,
            step_size=args.step_size,
            ngram_size=args.ngram_size,
            max_query_ngrams=args.max_query_ngrams,
            weight_coarse=args.weight_coarse,
            seed_term_count=args.seed_term_count,
            disable_semantic_recall=args.disable_semantic_recall,
            semantic_config=semantic_config,
        )
    )

    print(f"OK detection_mode={payload['detection_mode']}")
    print(f"OK detection_mode_label={payload['detection_mode_label']}")
    print(f"OK coarse_target_count={payload['coarse']['target_count']}")
    print(f"OK coarse_merged_results={len(payload['coarse']['merged_results'])}")
    print(f"OK lexical_candidates={len(payload['coarse']['results'])}")
    if payload["rewrite_detection"]["enabled"]:
        print(f"OK semantic_status={payload['rewrite_detection']['status']}")
        print(f"OK semantic_candidates={payload['coarse']['semantic']['candidate_count']}")
    print(f"OK compare_candidates={payload['fine']['candidate_count']}")
    print(f"OK sliced_candidates={payload['fine']['sliced_candidate_count']}")
    print(f"OK fine_results={payload['fine']['compared_candidate_count']}")
    if payload["rewrite_detection"]["enabled"]:
        print(f"OK rewrite_suspicious_results={payload['rewrite_detection']['suspicious_result_count']}")

    for rank, item in enumerate(payload["fine"]["results"], start=1):
        best_match = item["best_match"]
        print(
            "MATCH "
            f"rank={rank} "
            f"fine_score={item['fine_score']:.6f} "
            f"review_label={item['review_label']} "
            f"coarse_rank={item['coarse_rank']} "
            f"coarse_final_score={item['coarse_final_score']:.6f} "
            f"exact_substring_hit={int(bool(best_match['exact_substring_hit']))} "
            f"longest_match_ratio={best_match['longest_match_ratio']:.6f} "
            f"ngram_recall={best_match['ngram_recall']:.6f} "
            f"jaccard={best_match['jaccard']:.6f} "
            f"sequence_ratio={best_match['sequence_ratio']:.6f} "
            f"dataset_key={item['dataset_key']} "
            f"book_ext_id={item['book_ext_id']} "
            f"chapter_uid={item['chapter_uid']} "
            f"chapter_ext_id={item['chapter_ext_id']} "
            f"window_order={best_match['candidate_window_order']} "
            f"start_offset={best_match['candidate_start_offset']} "
            f"end_offset={best_match['candidate_end_offset']} "
            f"book_name={json.dumps(item['book_name'], ensure_ascii=False)} "
            f"chapter_name={json.dumps(item['chapter_name'], ensure_ascii=False)}"
        )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"OK json_out={args.json_out}")

    if args.csv_out:
        write_review_csv(args.csv_out, payload["fine"]["review_rows"])
        print(f"OK csv_out={args.csv_out}")


if __name__ == "__main__":
    main()
