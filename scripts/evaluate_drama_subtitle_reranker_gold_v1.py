from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import SETTINGS  # noqa: E402
from scripts.v2_common import connect_db  # noqa: E402
from service.drama_subtitle_hybrid_retrieval import search_drama_subtitle_hybrid_candidates  # noqa: E402
from service.drama_subtitle_retrieval import search_drama_subtitle_lexical_candidates  # noqa: E402
from service.drama_subtitle_reranker import (  # noqa: E402
    DramaSubtitleRerankerConfig,
    DramaSubtitleRerankerError,
    rerank_drama_subtitle_candidates,
)


DEFAULT_INPUT = "data_samples/batch_uploads/drama_subtitle_browser_smoke_26_20260803.csv"
DEFAULT_OUTPUT = "docs/81_drama_subtitle_reranker_gold_eval_2026-08-04.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate lexical, hybrid and reranker ranking on known subtitle sources.")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--window-limit", type=int, default=200)
    parser.add_argument("--reranker-model", default="bce-reranker-base_v1")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else (ROOT_DIR / path).resolve()


def build_title_to_book_ids(db_path: Path) -> dict[str, set[str]]:
    conn = connect_db(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT book_id, book_name FROM drama_subtitle_windows").fetchall()
    finally:
        conn.close()
    mapping: dict[str, set[str]] = {}
    for book_id, book_name in rows:
        mapping.setdefault(str(book_name or ""), set()).add(str(book_id or ""))
    return mapping


def candidate_rank(candidates: list[dict[str, Any]], expected_ids: set[str]) -> int | None:
    for rank, candidate in enumerate(candidates, start=1):
        if str(candidate.get("book_id") or "") in expected_ids:
            return rank
    return None


def hit_at(rank: int | None, limit: int) -> bool:
    return rank is not None and rank <= limit


def main() -> None:
    args = parse_args()
    if args.candidate_limit <= 0 or args.window_limit <= 0:
        raise ValueError("candidate and window limits must be > 0")
    input_path = resolve_path(args.input)
    output_path = resolve_path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(f"gold input not found: {input_path}")

    db_path = Path(SETTINGS.drama_subtitle_db_path)
    if not db_path.is_absolute():
        db_path = (ROOT_DIR / db_path).resolve()
    title_to_book_ids = build_title_to_book_ids(db_path)
    reranker_config = DramaSubtitleRerankerConfig(
        model=args.reranker_model,
        gitee_token=SETTINGS.drama_subtitle_semantic_gitee_token,
        gitee_token_env=SETTINGS.drama_subtitle_semantic_gitee_token_env,
    )

    rows = list(csv.DictReader(input_path.open("r", encoding="utf-8-sig", newline="")))
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        query_text = str(row.get("query_text") or "").strip()
        expected_title = str(row.get("source_display_title") or "").strip()
        expected_ids = title_to_book_ids.get(expected_title, set())
        item: dict[str, Any] = {
            "row_index": index,
            "source_display_title": expected_title,
            "expected_book_ids": sorted(expected_ids),
            "query_char_count": len(query_text),
            "query_text": query_text,
        }
        try:
            started = time.perf_counter()
            lexical = search_drama_subtitle_lexical_candidates(
                db_path=db_path,
                query_text=query_text,
                candidate_limit=args.candidate_limit,
                window_limit=args.window_limit,
                include_window_text=True,
            )
            item["lexical_duration_seconds"] = round(time.perf_counter() - started, 4)
            item["lexical_rank"] = candidate_rank(list(lexical.get("candidates") or []), expected_ids)
            item["lexical_candidate_count"] = int(lexical.get("candidate_count") or 0)

            started = time.perf_counter()
            hybrid = search_drama_subtitle_hybrid_candidates(
                db_path=db_path,
                query_text=query_text,
                candidate_limit=args.candidate_limit,
                window_limit=args.window_limit,
                include_window_text=True,
                semantic_config=SETTINGS.build_drama_subtitle_semantic_config(),
            )
            item["hybrid_duration_seconds"] = round(time.perf_counter() - started, 4)
            hybrid_candidates = list(hybrid.get("candidates") or [])
            item["hybrid_status"] = hybrid.get("semantic_status")
            item["hybrid_rank"] = candidate_rank(hybrid_candidates, expected_ids)
            item["hybrid_candidate_count"] = int(hybrid.get("candidate_count") or 0)
            item["semantic_candidate_count"] = int(hybrid.get("semantic_candidate_count") or 0)

            started = time.perf_counter()
            reranked = rerank_drama_subtitle_candidates(
                query_text=query_text,
                candidates=hybrid_candidates,
                config=reranker_config,
            )
            item["reranker_duration_seconds"] = round(time.perf_counter() - started, 4)
            item["reranker_status"] = "ok"
            item["reranker_rank"] = candidate_rank(reranked, expected_ids)
        except DramaSubtitleRerankerError as exc:
            item["reranker_status"] = "error"
            item["reranker_error"] = str(exc)
        except Exception as exc:  # Keep one bad sample from hiding the rest of the evaluation.
            item["evaluation_error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
        print(
            f"PROGRESS row={index}/{len(rows)} title={expected_title} "
            f"lexical_rank={item.get('lexical_rank')} hybrid_rank={item.get('hybrid_rank')} "
            f"reranker_rank={item.get('reranker_rank')} status={item.get('reranker_status', 'error')}"
        )

    summary: dict[str, Any] = {
        "input": str(input_path),
        "database": str(db_path),
        "candidate_limit": args.candidate_limit,
        "window_limit": args.window_limit,
        "reranker_model": args.reranker_model,
        "gold_definition": "source_display_title maps to known book_id in the subtitle database; this measures source recovery, not legal infringement truth.",
        "sample_count": len(results),
        "metrics": {},
        "results": results,
    }
    for method, field in (("lexical", "lexical_rank"), ("hybrid", "hybrid_rank"), ("reranker", "reranker_rank")):
        ranks = [item[field] for item in results if isinstance(item.get(field), int)]
        durations = [float(item[f"{method}_duration_seconds"]) for item in results if f"{method}_duration_seconds" in item]
        summary["metrics"][method] = {
            "evaluated_count": len(ranks),
            "top1": sum(hit_at(rank, 1) for rank in ranks),
            "top5": sum(hit_at(rank, 5) for rank in ranks),
            "top10": sum(hit_at(rank, 10) for rank in ranks),
            "average_rank": round(sum(ranks) / len(ranks), 4) if ranks else None,
            "average_duration_seconds": round(sum(durations) / len(durations), 4) if durations else None,
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "metrics": summary["metrics"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
