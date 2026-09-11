from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.business_store import get_compare_result, list_compare_results
from service.review_export import build_review_export_xlsx_with_metrics


DEFAULT_BUSINESS_DB = "data/novel_similarity_web_v1.sqlite3"
DEFAULT_RETRIEVAL_DB = "data/novel_similarity_v2.sqlite3"
DEFAULT_EXPORT_ROOT = "runtime/benchmarks"

PROCESSED_REVIEW_STATUSES = (
    "confirmed_high_risk",
    "needs_followup",
    "false_positive",
)


@dataclass(frozen=True)
class BenchmarkTargets:
    owner_user_id: int
    result_id: int
    task_id: str
    processed_task_id: str
    largest_task_id: str


class BenchmarkError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark business DB read paths for the web workflow."
    )
    parser.add_argument("--business-db", default=DEFAULT_BUSINESS_DB)
    parser.add_argument("--retrieval-db", default=DEFAULT_RETRIEVAL_DB)
    parser.add_argument("--export-root", default=DEFAULT_EXPORT_ROOT)
    parser.add_argument("--result-id", type=int, default=0)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--owner-user-id", type=int, default=0)
    parser.add_argument("--detail-runs", type=int, default=5)
    parser.add_argument("--list-runs", type=int, default=5)
    parser.add_argument("--export-runs", type=int, default=2)
    parser.add_argument("--list-limit", type=int, default=50)
    parser.add_argument(
        "--export-processed-only",
        choices=["auto", "true", "false"],
        default="auto",
    )
    parser.add_argument("--output-json", default="")
    return parser.parse_args()


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summarize_runs(name: str, elapsed_values: list[float]) -> dict[str, Any]:
    if not elapsed_values:
        return {
            "name": name,
            "runs": 0,
            "min_seconds": None,
            "max_seconds": None,
            "avg_seconds": None,
            "median_seconds": None,
            "p95_seconds": None,
            "samples_seconds": [],
        }
    return {
        "name": name,
        "runs": len(elapsed_values),
        "min_seconds": round(min(elapsed_values), 6),
        "max_seconds": round(max(elapsed_values), 6),
        "avg_seconds": round(statistics.fmean(elapsed_values), 6),
        "median_seconds": round(statistics.median(elapsed_values), 6),
        "p95_seconds": round(_percentile(elapsed_values, 0.95), 6),
        "samples_seconds": [round(value, 6) for value in elapsed_values],
    }


def _discover_targets(
    business_db_path: str | Path,
    *,
    owner_user_id: int = 0,
    result_id: int = 0,
    task_id: str = "",
) -> BenchmarkTargets:
    conn = _connect(business_db_path)
    try:
        owner_filter = " AND t.owner_user_id = ?"
        owner_params: list[Any] = [int(owner_user_id)] if owner_user_id > 0 else []

        if result_id > 0:
            row = conn.execute(
                f"""
                SELECT i.result_id,
                       i.task_id,
                       t.owner_user_id
                  FROM compare_task_items i
                  JOIN compare_tasks t
                    ON t.task_id = i.task_id
                 WHERE i.result_id = ?
                   AND COALESCE(t.is_deleted, 0) = 0
                """,
                (int(result_id),),
            ).fetchone()
            if row is None:
                raise BenchmarkError(f"Result not found: {result_id}")
            if owner_user_id <= 0:
                owner_user_id = int(row["owner_user_id"] or 0)
            if not task_id:
                task_id = str(row["task_id"] or "")

        if owner_user_id <= 0 or not task_id:
            latest_row = conn.execute(
                f"""
                SELECT i.result_id,
                       i.task_id,
                       t.owner_user_id
                  FROM compare_task_items i
                  JOIN compare_tasks t
                    ON t.task_id = i.task_id
                 WHERE i.status = 'completed'
                   AND COALESCE(t.is_deleted, 0) = 0
                   AND t.owner_user_id IS NOT NULL
                   {owner_filter if owner_user_id > 0 else ""}
                 ORDER BY i.updated_at DESC, i.result_id DESC
                 LIMIT 1
                """,
                tuple(owner_params),
            ).fetchone()
            if latest_row is None:
                raise BenchmarkError("No completed result rows found in the business DB.")
            if owner_user_id <= 0:
                owner_user_id = int(latest_row["owner_user_id"] or 0)
            if result_id <= 0:
                result_id = int(latest_row["result_id"] or 0)
            if not task_id:
                task_id = str(latest_row["task_id"] or "")

        processed_task_row = conn.execute(
            """
            SELECT i.task_id,
                   COUNT(*) AS processed_count
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
              JOIN compare_task_reviews r
                ON r.result_id = i.result_id
             WHERE i.status = 'completed'
               AND COALESCE(t.is_deleted, 0) = 0
               AND t.owner_user_id = ?
               AND r.review_status IN (?, ?, ?)
             GROUP BY i.task_id
             ORDER BY MAX(i.updated_at) DESC, processed_count DESC
             LIMIT 1
            """,
            (int(owner_user_id), *PROCESSED_REVIEW_STATUSES),
        ).fetchone()

        processed_task_id = (
            str(processed_task_row["task_id"] or "")
            if processed_task_row is not None
            else ""
        )
        largest_task_row = conn.execute(
            """
            SELECT i.task_id,
                   COUNT(*) AS completed_count
              FROM compare_task_items i
              JOIN compare_tasks t
                ON t.task_id = i.task_id
             WHERE i.status = 'completed'
               AND COALESCE(t.is_deleted, 0) = 0
               AND t.owner_user_id = ?
             GROUP BY i.task_id
             ORDER BY completed_count DESC, MAX(i.updated_at) DESC
             LIMIT 1
            """,
            (int(owner_user_id),),
        ).fetchone()
        largest_task_id = (
            str(largest_task_row["task_id"] or "")
            if largest_task_row is not None
            else str(task_id or "")
        )
        if owner_user_id <= 0 or result_id <= 0 or not task_id:
            raise BenchmarkError("Failed to discover benchmark targets.")
        return BenchmarkTargets(
            owner_user_id=int(owner_user_id),
            result_id=int(result_id),
            task_id=str(task_id),
            processed_task_id=str(processed_task_id),
            largest_task_id=str(largest_task_id),
        )
    finally:
        conn.close()


def _fetch_result_metadata(
    business_db_path: str | Path,
    result_id: int,
) -> dict[str, Any]:
    conn = _connect(business_db_path)
    try:
        row = conn.execute(
            """
            SELECT i.result_id,
                   i.task_id,
                   i.query_text_preview,
                   i.top1_book_name,
                   i.top1_fine_score,
                   i.duration_seconds,
                   COALESCE(LENGTH(p.query_text), LENGTH(i.query_text), 0) AS query_text_length,
                   COALESCE(LENGTH(p.result_payload_json), LENGTH(i.result_payload_json), 0) AS payload_json_length
              FROM compare_task_items i
              LEFT JOIN compare_task_item_payloads p
                ON p.result_id = i.result_id
             WHERE i.result_id = ?
            """,
            (int(result_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    return {
        "result_id": int(row["result_id"]),
        "task_id": str(row["task_id"] or ""),
        "query_text_preview": str(row["query_text_preview"] or ""),
        "top1_book_name": str(row["top1_book_name"] or ""),
        "top1_fine_score": None if row["top1_fine_score"] is None else float(row["top1_fine_score"]),
        "duration_seconds": None if row["duration_seconds"] is None else float(row["duration_seconds"]),
        "query_text_length": int(row["query_text_length"] or 0),
        "payload_json_length": int(row["payload_json_length"] or 0),
    }


def _run_detail_benchmark(
    business_db_path: str | Path,
    retrieval_db_path: str | Path,
    *,
    result_id: int,
    owner_user_id: int,
    runs: int,
) -> dict[str, Any]:
    elapsed_values: list[float] = []
    result_shape: dict[str, Any] = {}
    for _ in range(max(int(runs), 1)):
        started = time.perf_counter()
        result = get_compare_result(
            business_db_path,
            result_id,
            retrieval_db_path=retrieval_db_path,
            owner_user_id=owner_user_id,
        )
        elapsed = time.perf_counter() - started
        elapsed_values.append(elapsed)
        if result and not result_shape:
            fine_results = (
                dict(result.get("result_payload") or {})
                .get("fine", {})
                .get("results", [])
            )
            result_shape = {
                "query_text_length": len(str(result.get("query_text") or "")),
                "fine_result_count": len(fine_results) if isinstance(fine_results, list) else 0,
                "has_review_context_text": bool(
                    isinstance(fine_results, list)
                    and fine_results
                    and isinstance(fine_results[0], dict)
                    and isinstance(fine_results[0].get("best_match"), dict)
                    and str(
                        fine_results[0]["best_match"].get("candidate_review_context_text") or ""
                    ).strip()
                ),
            }
    summary = _summarize_runs("get_compare_result", elapsed_values)
    summary["result_shape"] = result_shape
    return summary


def _run_list_benchmark(
    business_db_path: str | Path,
    *,
    owner_user_id: int,
    task_id: str,
    runs: int,
    limit: int,
) -> dict[str, Any]:
    elapsed_values: list[float] = []
    result_shape: dict[str, Any] = {}
    for _ in range(max(int(runs), 1)):
        started = time.perf_counter()
        items = list_compare_results(
            db_path=business_db_path,
            limit=max(int(limit), 1),
            offset=0,
            task_id=task_id,
            item_status="completed",
            sort_by="score_desc",
            owner_user_id=owner_user_id,
            exclude_cleared=True,
            candidate_score_threshold=0.01,
        )
        elapsed = time.perf_counter() - started
        elapsed_values.append(elapsed)
        if not result_shape:
            result_shape = {
                "returned_items": len(items),
                "top_result_id": int(items[0]["result_id"]) if items else None,
                "top_score": float(items[0]["top1_fine_score"]) if items and items[0]["top1_fine_score"] is not None else None,
            }
    summary = _summarize_runs("list_compare_results", elapsed_values)
    summary["result_shape"] = result_shape
    return summary


def _run_export_benchmark(
    business_db_path: str | Path,
    retrieval_db_path: str | Path,
    export_root: str | Path,
    *,
    owner_user_id: int,
    task_id: str,
    runs: int,
    processed_only: bool,
) -> dict[str, Any]:
    elapsed_values: list[float] = []
    export_shape: dict[str, Any] = {}
    benchmark_export_root = (ROOT / export_root).resolve()
    benchmark_export_root.mkdir(parents=True, exist_ok=True)
    generated_files: list[str] = []
    for _ in range(max(int(runs), 1)):
        started = time.perf_counter()
        export_path, download_name, export_metrics = build_review_export_xlsx_with_metrics(
            business_db_path=business_db_path,
            retrieval_db_path=retrieval_db_path,
            export_root=benchmark_export_root,
            owner_user_id=owner_user_id,
            task_id=task_id,
            item_status="completed",
            sort_by="score_desc",
            processed_only=processed_only,
            candidate_score_threshold=0.01,
        )
        elapsed = time.perf_counter() - started
        elapsed_values.append(elapsed)
        file_size = export_path.stat().st_size if export_path.exists() else 0
        generated_files.append(str(export_path))
        if not export_shape:
            export_shape = {
                "download_name": download_name,
                "file_size_bytes": int(file_size),
                "summary_count": int(export_metrics.get("summary_count") or 0),
                "detail_count": int(export_metrics.get("detail_count") or 0),
                "stage_metrics": dict(export_metrics.get("stages") or {}),
            }
    for path_str in generated_files:
        path = Path(path_str)
        if path.exists():
            path.unlink()
    summary = _summarize_runs("build_review_export_xlsx", elapsed_values)
    summary["result_shape"] = export_shape
    summary["processed_only"] = processed_only
    return summary


def main() -> int:
    args = parse_args()
    business_db_path = (ROOT / args.business_db).resolve()
    retrieval_db_path = (ROOT / args.retrieval_db).resolve()
    if not business_db_path.exists():
        print(f"Business DB not found: {business_db_path}", file=sys.stderr)
        return 2
    if not retrieval_db_path.exists():
        print(f"Retrieval DB not found: {retrieval_db_path}", file=sys.stderr)
        return 2

    targets = _discover_targets(
        business_db_path,
        owner_user_id=int(args.owner_user_id or 0),
        result_id=int(args.result_id or 0),
        task_id=str(args.task_id or ""),
    )
    result_metadata = _fetch_result_metadata(
        business_db_path,
        targets.result_id,
    )
    if args.export_processed_only == "true":
        export_processed_only = True
    elif args.export_processed_only == "false":
        export_processed_only = False
    else:
        export_processed_only = bool(targets.processed_task_id)
    export_task_id = (
        targets.processed_task_id
        if export_processed_only and targets.processed_task_id
        else targets.largest_task_id
    )

    report = {
        "ok": True,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "business_db_path": str(business_db_path),
        "retrieval_db_path": str(retrieval_db_path),
        "targets": {
            "owner_user_id": targets.owner_user_id,
            "result_id": targets.result_id,
            "task_id": targets.task_id,
            "processed_task_id": targets.processed_task_id,
            "largest_task_id": targets.largest_task_id,
            "export_task_id": export_task_id,
            "export_processed_only": export_processed_only,
        },
        "result_metadata": result_metadata,
        "benchmarks": {
            "detail": _run_detail_benchmark(
                business_db_path,
                retrieval_db_path,
                result_id=targets.result_id,
                owner_user_id=targets.owner_user_id,
                runs=args.detail_runs,
            ),
            "result_list": _run_list_benchmark(
                business_db_path,
                owner_user_id=targets.owner_user_id,
                task_id=targets.task_id,
                runs=args.list_runs,
                limit=args.list_limit,
            ),
            "review_export": _run_export_benchmark(
                business_db_path,
                retrieval_db_path,
                args.export_root,
                owner_user_id=targets.owner_user_id,
                task_id=export_task_id,
                runs=args.export_runs,
                processed_only=export_processed_only,
            ),
        },
    }

    output_json = args.output_json.strip()
    if output_json:
        output_path = (ROOT / output_json).resolve()
    else:
        output_path = (
            ROOT
            / "docs"
            / f"58_web读路径性能基线_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
        ).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Saved benchmark report to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
