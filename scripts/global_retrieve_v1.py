from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.global_retrieval import global_retrieve_candidates


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
    parser.add_argument("--candidate-limit-per-index", type=int, default=200)
    parser.add_argument("--merged-top-k", type=int, default=20)
    parser.add_argument("--ngram-size", type=int, default=3)
    parser.add_argument("--max-query-ngrams", type=int, default=120)
    parser.add_argument("--weight-coarse", type=float, default=0.25)
    parser.add_argument("--seed-term-count", type=int, default=12)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    query_text = load_query_text(args.query_text, args.query_file)
    payload = global_retrieve_candidates(
        db_path=args.db,
        query_text=query_text,
        candidate_limit_per_index=args.candidate_limit_per_index,
        merged_top_k=args.merged_top_k,
        ngram_size=args.ngram_size,
        max_query_ngrams=args.max_query_ngrams,
        weight_coarse=args.weight_coarse,
        seed_term_count=args.seed_term_count,
    )

    print(f"OK target_count={payload['target_count']}")
    print(f"OK merged_results={len(payload['results'])}")

    for result in payload["index_results"]:
        dataset_key = result["dataset_key"]
        table_name = result["table_name"]
        candidate_count = result["payload"]["candidate_count"]
        print(
            "INDEX "
            f"dataset_key={dataset_key} "
            f"table_name={table_name} "
            f"candidate_count={candidate_count}"
        )

    for rank, item in enumerate(payload["results"], start=1):
        print(
            "RESULT "
            f"rank={rank} "
            f"final_score={item['final_score']:.6f} "
            f"ngram_score={item['ngram_score']:.6f} "
            f"dataset_key={item['dataset_key']} "
            f"book_ext_id={item['book_ext_id']} "
            f"chapter_uid={item['chapter_uid']} "
            f"chapter_ext_id={item['chapter_ext_id']} "
            f"source_table_name={item['source_table_name']} "
            f"book_name={json.dumps(item['book_name'], ensure_ascii=False)} "
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
