from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import service.task_executor as task_executor_module
from service.business_store import create_compare_task, init_business_db, list_compare_tasks
from service.semantic_retrieval import SemanticRetrievalConfig
from service.task_executor import run_next_queued_task


DEFAULT_BENCHMARK_ROOT = "runtime/benchmarks/task_worker_concurrency"


@dataclass
class TimingMetric:
    call_count: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0


class TimingRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, TimingMetric] = {}

    def record(self, name: str, elapsed_seconds: float) -> None:
        with self._lock:
            metric = self._metrics.setdefault(name, TimingMetric())
            metric.call_count += 1
            metric.total_seconds += elapsed_seconds
            metric.max_seconds = max(metric.max_seconds, elapsed_seconds)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            return {
                name: {
                    "call_count": metric.call_count,
                    "total_seconds": round(metric.total_seconds, 6),
                    "avg_milliseconds": round(
                        (metric.total_seconds / metric.call_count) * 1000.0,
                        3,
                    )
                    if metric.call_count > 0
                    else 0.0,
                    "max_milliseconds": round(metric.max_seconds * 1000.0, 3),
                }
                for name, metric in sorted(self._metrics.items())
            }

    def total_seconds_for(self, names: list[str]) -> float:
        with self._lock:
            return sum(
                self._metrics.get(name, TimingMetric()).total_seconds
                for name in names
            )


def _wrap_timed(
    recorder: TimingRecorder,
    metric_name: str,
    fn: Callable[..., Any],
) -> Callable[..., Any]:
    def _wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
        started_at = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            recorder.record(metric_name, time.perf_counter() - started_at)

    return _wrapped


@contextmanager
def _patch_task_executor_timing(recorder: TimingRecorder):
    patches = [
        ("claim_next_compare_task", _wrap_timed(recorder, "claim_next_compare_task", task_executor_module.claim_next_compare_task)),
        ("count_compare_task_items", _wrap_timed(recorder, "count_compare_task_items", task_executor_module.count_compare_task_items)),
        ("get_compare_task", _wrap_timed(recorder, "get_compare_task", task_executor_module.get_compare_task)),
        ("get_compare_task_runtime_state", _wrap_timed(recorder, "get_compare_task_runtime_state", task_executor_module.get_compare_task_runtime_state)),
        ("list_compare_task_input_items", _wrap_timed(recorder, "list_compare_task_input_items", task_executor_module.list_compare_task_input_items)),
        ("list_compare_task_items", _wrap_timed(recorder, "list_compare_task_items", task_executor_module.list_compare_task_items)),
        ("list_compare_task_result_payloads", _wrap_timed(recorder, "list_compare_task_result_payloads", task_executor_module.list_compare_task_result_payloads)),
        ("mark_task_items_running", _wrap_timed(recorder, "mark_task_items_running", task_executor_module.mark_task_items_running)),
        ("replace_task_items", _wrap_timed(recorder, "replace_task_items", task_executor_module.replace_task_items)),
        ("set_task_input_count", _wrap_timed(recorder, "set_task_input_count", task_executor_module.set_task_input_count)),
        ("update_task_progress", _wrap_timed(recorder, "update_task_progress", task_executor_module.update_task_progress)),
        ("save_task_item_outcomes_batch", _wrap_timed(recorder, "save_task_item_outcomes_batch", task_executor_module.save_task_item_outcomes_batch)),
        ("finish_task", _wrap_timed(recorder, "finish_task", task_executor_module.finish_task)),
        ("parse_task_input_file", _wrap_timed(recorder, "parse_task_input_file", task_executor_module.parse_task_input_file)),
        ("_mark_task_item_running", _wrap_timed(recorder, "_mark_task_item_running", task_executor_module._mark_task_item_running)),
    ]
    originals: list[tuple[str, Any]] = []
    try:
        for attr_name, wrapped in patches:
            originals.append((attr_name, getattr(task_executor_module, attr_name)))
            setattr(task_executor_module, attr_name, wrapped)
        yield
    finally:
        for attr_name, original in originals:
            setattr(task_executor_module, attr_name, original)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark synthetic task-worker concurrency using local fake compare work.",
    )
    parser.add_argument("--benchmark-root", default=DEFAULT_BENCHMARK_ROOT)
    parser.add_argument("--task-count", type=int, default=4)
    parser.add_argument("--items-per-task", type=int, default=8)
    parser.add_argument("--item-parallelism", type=int, default=2)
    parser.add_argument("--worker-counts", default="1,2")
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    parser.add_argument("--output-json", default="")
    return parser.parse_args()


def _build_fake_compare_result(query_text: str, candidate_display_score_threshold: float | None) -> dict[str, Any]:
    return {
        "query_text": query_text,
        "detection_mode": "rewrite",
        "detection_mode_label": "测试",
        "detection_mode_description": "Synthetic benchmark compare pipeline",
        "params": {
            "candidate_display_score_threshold": candidate_display_score_threshold,
        },
        "capabilities": {
            "reuse_detection_enabled": True,
            "rewrite_detection_enabled": True,
            "semantic_recall_enabled": False,
        },
        "coarse": {
            "target_count": 1,
            "candidate_limit_per_index": 10,
            "merged_top_k": 5,
            "index_summaries": [],
            "semantic": {
                "status": "fallback_lexical_only",
                "candidate_count": 0,
                "error": "",
                "results": [],
            },
            "merged_results": [],
            "results": [],
        },
        "reuse_detection": {
            "enabled": True,
            "result_count": 1,
            "strong_evidence_count_in_pool": 1,
            "results": [
                {
                    "book_name": "Synthetic Novel",
                    "chapter_name": "Chapter 1",
                    "review_label": "强证据",
                    "confidence_label": "strong",
                    "fine_score": 0.91,
                }
            ],
            "strong_evidence_results": [],
        },
        "rewrite_detection": {
            "enabled": True,
            "status": "fallback_lexical_only",
            "semantic_recall_enabled": False,
            "message": "synthetic benchmark fallback",
            "semantic_candidate_count": 0,
            "suspicious_result_count": 0,
            "results": [],
        },
        "fine": {
            "candidate_count": 1,
            "sliced_candidate_count": 1,
            "compared_candidate_count": 1,
            "results": [
                {
                    "fine_rank": 1,
                    "review_label": "强证据",
                    "confidence_label": "strong",
                    "fine_score": 0.91,
                    "coarse_rank": 1,
                    "coarse_final_score": 0.81,
                    "dataset_key": "synthetic",
                    "book_ext_id": "synthetic-book-1",
                    "book_name": "Synthetic Novel",
                    "chapter_uid": 1,
                    "chapter_ext_id": "1",
                    "chapter_name": "Chapter 1",
                    "best_match": {
                        "candidate_window_order": 1,
                        "candidate_start_offset": 0,
                        "candidate_end_offset": min(len(query_text), 200),
                        "exact_substring_hit": True,
                        "longest_match_len": min(len(query_text), 120),
                        "longest_match_ratio": 1.0,
                        "ngram_recall": 1.0,
                        "ngram_precision": 1.0,
                        "jaccard": 1.0,
                        "sequence_ratio": 1.0,
                        "matched_substring": query_text[:120],
                        "query_text_preview": query_text[:30],
                        "candidate_text_preview": query_text[:30],
                        "candidate_text": query_text[:200],
                    },
                }
            ],
            "review_rows": [
                {
                    "detection_mode": "rewrite",
                    "fine_rank": 1,
                    "review_label": "强证据",
                    "confidence_label": "strong",
                    "fine_score": 0.91,
                    "coarse_rank": 1,
                    "coarse_final_score": 0.81,
                    "dataset_key": "synthetic",
                    "book_ext_id": "synthetic-book-1",
                    "book_name": "Synthetic Novel",
                    "chapter_uid": 1,
                    "chapter_ext_id": "1",
                    "chapter_name": "Chapter 1",
                    "candidate_window_order": 1,
                    "candidate_start_offset": 0,
                    "candidate_end_offset": min(len(query_text), 200),
                    "exact_substring_hit": True,
                    "longest_match_len": min(len(query_text), 120),
                    "longest_match_ratio": 1.0,
                    "ngram_recall": 1.0,
                    "ngram_precision": 1.0,
                    "jaccard": 1.0,
                    "sequence_ratio": 1.0,
                    "matched_substring": query_text[:120],
                    "query_text_preview": query_text[:30],
                    "candidate_text_preview": query_text[:30],
                    "query_text": query_text,
                    "candidate_text": query_text[:200],
                }
            ],
        },
    }


def _create_fake_compare_fn(
    *,
    sleep_seconds: float,
    active_counter: dict[str, int],
    lock: threading.Lock,
    recorder: TimingRecorder,
):
    def _fake_compare(request):  # type: ignore[no-untyped-def]
        started_at = time.perf_counter()
        with lock:
            active_counter["active"] += 1
            active_counter["max_active"] = max(
                active_counter["max_active"],
                active_counter["active"],
            )
            active_counter["calls"] += 1
        try:
            time.sleep(max(float(sleep_seconds), 0.0))
            return _build_fake_compare_result(
                str(request.query_text),
                request.candidate_display_score_threshold,
            )
        finally:
            with lock:
                active_counter["active"] -= 1
            recorder.record("compare_fn", time.perf_counter() - started_at)

    return _fake_compare


def _seed_tasks(
    *,
    business_db_path: Path,
    task_count: int,
    items_per_task: int,
    input_root: Path,
) -> None:
    input_root.mkdir(parents=True, exist_ok=True)
    for task_index in range(task_count):
        input_path = input_root / f"benchmark_task_{task_index + 1:02d}.txt"
        lines = [
            f"task {task_index + 1} item {item_index + 1} synthetic benchmark text"
            for item_index in range(items_per_task)
        ]
        input_path.write_text("\n".join(lines), encoding="utf-8")
        create_compare_task(
            db_path=business_db_path,
            task_id=f"benchmark-task-{task_index + 1:02d}",
            detection_mode="rewrite",
            source_file_name=input_path.name,
            source_file_ext=".txt",
            source_file_path=str(input_path),
            source_file_sha256=f"benchmark-task-{task_index + 1:02d}",
            source_file_size=input_path.stat().st_size,
            params={"candidate_display_score_threshold": 0.01},
            created_by="benchmark",
        )


def _run_worker_loop(
    *,
    worker_name: str,
    business_db_path: Path,
    retrieval_db_path: Path,
    export_root: Path,
    item_parallelism: int,
    compare_fn,
    processed_task_ids: list[str],
    worker_errors: list[str],
) -> None:
    try:
        while True:
            task = run_next_queued_task(
                business_db_path=business_db_path,
                retrieval_db_path=retrieval_db_path,
                semantic_config=SemanticRetrievalConfig(),
                export_root=export_root,
                worker_name=worker_name,
                item_parallelism=item_parallelism,
                compare_fn=compare_fn,
            )
            if task is None:
                return
            processed_task_ids.append(str(task["task_id"]))
    except Exception as exc:  # pragma: no cover - benchmark safety
        worker_errors.append(f"{worker_name}: {exc}")


def _run_scenario(
    *,
    benchmark_root: Path,
    worker_count: int,
    task_count: int,
    items_per_task: int,
    item_parallelism: int,
    sleep_seconds: float,
) -> dict[str, Any]:
    scenario_root = benchmark_root / f"workers_{worker_count}"
    if scenario_root.exists():
        for path in sorted(scenario_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    scenario_root.mkdir(parents=True, exist_ok=True)

    business_db_path = scenario_root / "business.sqlite3"
    retrieval_db_path = scenario_root / "retrieval_unused.sqlite3"
    export_root = scenario_root / "exports"
    init_business_db(business_db_path)
    _seed_tasks(
        business_db_path=business_db_path,
        task_count=task_count,
        items_per_task=items_per_task,
        input_root=scenario_root / "inputs",
    )

    active_counter = {"active": 0, "max_active": 0, "calls": 0}
    lock = threading.Lock()
    timing_recorder = TimingRecorder()
    compare_fn = _create_fake_compare_fn(
        sleep_seconds=sleep_seconds,
        active_counter=active_counter,
        lock=lock,
        recorder=timing_recorder,
    )

    processed_task_ids: list[str] = []
    worker_errors: list[str] = []
    threads = [
        threading.Thread(
            target=_run_worker_loop,
            kwargs={
                "worker_name": f"benchmark-worker-{index + 1}",
                "business_db_path": business_db_path,
                "retrieval_db_path": retrieval_db_path,
                "export_root": export_root,
                "item_parallelism": item_parallelism,
                "compare_fn": compare_fn,
                "processed_task_ids": processed_task_ids,
                "worker_errors": worker_errors,
            },
            daemon=True,
        )
        for index in range(worker_count)
    ]

    started_at = time.perf_counter()
    with _patch_task_executor_timing(timing_recorder):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
    wall_seconds = time.perf_counter() - started_at

    task_rows = list_compare_tasks(business_db_path, limit=task_count + 4, offset=0)
    completed_tasks = [task for task in task_rows if str(task.get("status") or "") == "completed"]
    accepted_total = sum(int((task.get("counts") or {}).get("accepted", 0) or 0) for task in task_rows)
    completed_total = sum(int((task.get("counts") or {}).get("completed", 0) or 0) for task in task_rows)
    failed_total = sum(int((task.get("counts") or {}).get("failed", 0) or 0) for task in task_rows)
    hot_write_metric_names = [
        "_mark_task_item_running",
        "mark_task_items_running",
        "replace_task_items",
        "save_task_item_outcomes_batch",
        "set_task_input_count",
        "update_task_progress",
        "finish_task",
    ]
    hot_read_metric_names = [
        "count_compare_task_items",
        "get_compare_task",
        "get_compare_task_runtime_state",
        "list_compare_task_input_items",
        "list_compare_task_items",
        "list_compare_task_result_payloads",
        "parse_task_input_file",
    ]
    task_control_metric_names = [
        "claim_next_compare_task",
    ]

    return {
        "worker_count": int(worker_count),
        "task_count": int(task_count),
        "items_per_task": int(items_per_task),
        "item_parallelism": int(item_parallelism),
        "sleep_seconds": float(sleep_seconds),
        "wall_seconds": round(wall_seconds, 6),
        "processed_task_ids": processed_task_ids,
        "processed_task_count": len(processed_task_ids),
        "task_statuses": {str(task["task_id"]): str(task["status"]) for task in task_rows},
        "all_tasks_completed": len(completed_tasks) == task_count and failed_total == 0,
        "accepted_total": accepted_total,
        "completed_total": completed_total,
        "failed_total": failed_total,
        "compare_call_count": int(active_counter["calls"]),
        "max_active_compare_calls": int(active_counter["max_active"]),
        "throughput_items_per_second": round(
            completed_total / wall_seconds,
            6,
        )
        if wall_seconds > 0
        else None,
        "timing_profile": {
            "metrics": timing_recorder.snapshot(),
            "group_totals": {
                "compare_seconds": round(
                    timing_recorder.total_seconds_for(["compare_fn"]),
                    6,
                ),
                "hot_write_seconds": round(
                    timing_recorder.total_seconds_for(hot_write_metric_names),
                    6,
                ),
                "hot_read_seconds": round(
                    timing_recorder.total_seconds_for(hot_read_metric_names),
                    6,
                ),
                "task_control_seconds": round(
                    timing_recorder.total_seconds_for(task_control_metric_names),
                    6,
                ),
            },
        },
        "worker_errors": worker_errors,
        "thread_timeout": any(thread.is_alive() for thread in threads),
    }


def main() -> int:
    args = parse_args()
    benchmark_root = (ROOT_DIR / args.benchmark_root).resolve()
    benchmark_root.mkdir(parents=True, exist_ok=True)
    worker_counts = [
        int(part.strip())
        for part in str(args.worker_counts or "").split(",")
        if part.strip()
    ]
    if not worker_counts:
        raise ValueError("No worker counts provided")

    scenarios = [
        _run_scenario(
            benchmark_root=benchmark_root,
            worker_count=worker_count,
            task_count=max(int(args.task_count), 1),
            items_per_task=max(int(args.items_per_task), 1),
            item_parallelism=max(int(args.item_parallelism), 1),
            sleep_seconds=max(float(args.sleep_seconds), 0.0),
        )
        for worker_count in worker_counts
    ]

    comparison: dict[str, Any] = {}
    if len(scenarios) >= 2:
        baseline = scenarios[0]
        target = scenarios[1]
        baseline_wall = float(baseline["wall_seconds"] or 0.0)
        target_wall = float(target["wall_seconds"] or 0.0)
        if baseline_wall > 0 and target_wall > 0:
            comparison = {
                "baseline_worker_count": int(baseline["worker_count"]),
                "target_worker_count": int(target["worker_count"]),
                "wall_seconds_delta": round(baseline_wall - target_wall, 6),
                "wall_seconds_improvement_ratio": round(
                    (baseline_wall - target_wall) / baseline_wall,
                    6,
                ),
                "throughput_gain_ratio": round(
                    (
                        float(target["throughput_items_per_second"] or 0.0)
                        - float(baseline["throughput_items_per_second"] or 0.0)
                    )
                    / float(baseline["throughput_items_per_second"] or 1.0),
                    6,
                )
                if float(baseline["throughput_items_per_second"] or 0.0) > 0
                else None,
            }

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark_root": str(benchmark_root),
        "scenarios": scenarios,
        "comparison": comparison,
    }

    output_json = str(args.output_json or "").strip()
    if output_json:
        output_path = (ROOT_DIR / output_json).resolve()
    else:
        output_path = (
            ROOT_DIR
            / "docs"
            / f"59_task_worker_concurrency_benchmark_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
        ).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Saved benchmark report to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
