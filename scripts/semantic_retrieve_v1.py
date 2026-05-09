from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def load_query_text(query_text: str, query_file: str) -> str:
    if query_text:
        return query_text
    if query_file:
        return Path(query_file).read_text(encoding="utf-8")
    raise ValueError("Either --query-text or --query-file is required")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--query-text", default="")
    parser.add_argument("--query-file", default="")
    parser.add_argument("--backend", default="remote", choices=["remote", "local"])
    parser.add_argument(
        "--remote-embedding-backend",
        default=DEFAULT_REMOTE_EMBEDDING_BACKEND,
        choices=["ollama", "gitee_api"],
    )
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
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
    parser.add_argument("--merged-top-k", type=int, default=20)
    parser.add_argument("--score-threshold", type=float, default=0.0)
    parser.add_argument("--local-index-dir", default="")
    parser.add_argument("--local-model-path", default="")
    parser.add_argument("--local-device", default="cpu")
    parser.add_argument("--local-encode-batch-size", type=int, default=16)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    query_text = load_query_text(args.query_text, args.query_file)
    config = SemanticRetrievalConfig(
        backend=args.backend,
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
        local_index_dir=args.local_index_dir,
        local_model_path=args.local_model_path,
        local_device=args.local_device,
        local_encode_batch_size=args.local_encode_batch_size,
    )
    payload = semantic_retrieve_candidates(
        db_path=args.db,
        query_text=query_text,
        config=config,
    )

    print(f"OK status={payload['status']}")
    print(f"OK query_chunk_count={payload['query_chunk_count']}")
    print(f"OK chapter_hit_count={payload['chapter_hit_count']}")
    print(f"OK chunk_hit_count={payload['chunk_hit_count']}")
    print(f"OK candidate_count={payload['candidate_count']}")

    for rank, item in enumerate(payload["results"], start=1):
        print(
            "RESULT "
            f"rank={rank} "
            f"final_score={item['final_score']:.6f} "
            f"semantic_chunk_score_max={item['semantic_chunk_score_max']:.6f} "
            f"semantic_chapter_score_max={item['semantic_chapter_score_max']:.6f} "
            f"semantic_hit_count={item['semantic_hit_count']} "
            f"dataset_key={item['dataset_key']} "
            f"book_ext_id={item['book_ext_id']} "
            f"book_name={json.dumps(item['book_name'], ensure_ascii=False)} "
            f"chapter_ext_id={item['chapter_ext_id']} "
            f"chapter_uid={item['chapter_uid']} "
            f"chapter_name={json.dumps(item['chapter_name'], ensure_ascii=False)}"
        )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"OK json_out={args.json_out}")


if __name__ == "__main__":
    main()
