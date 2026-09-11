from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from hashlib import sha256
from datetime import datetime
from pathlib import Path
import csv
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from service.business_store import (
    DEFAULT_TASK_RECOVERY_STALE_SECONDS,
    claim_next_compare_task,
    count_compare_task_items,
    connect_business_db,
    finish_task,
    get_compare_task,
    get_compare_task_runtime_state,
    list_compare_task_result_payloads,
    list_compare_task_input_items,
    list_compare_task_items,
    mark_task_paused,
    mark_task_items_running,
    requeue_running_task_items,
    replace_task_items,
    save_task_item_outcomes_batch,
    settle_compare_task_for_worker_shutdown,
    set_task_input_count,
    update_task_progress,
)
from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline
from service.semantic_retrieval import SemanticRetrievalConfig
from service.task_input_parser import parse_task_input_file


ROOT_DIR = Path(__file__).resolve().parent.parent
CompareFn = Callable[[ComparePipelineRequest], dict[str, Any]]
TASK_PROGRESS_PERSIST_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class TaskItemExecutionResult:
    item_order: int
    semantic_status: str
    top1_book_name: str
    top1_chapter_name: str
    top1_review_label: str
    top1_confidence_label: str
    top1_fine_score: float | None
    result_payload: dict[str, Any]


class _ShutdownAwareThreadPoolExecutor(ThreadPoolExecutor):
    def __init__(self, *args: Any, stop_event: threading.Event | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stop_event = stop_event

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        stopping = self._stop_event is not None and self._stop_event.is_set()
        self.shutdown(wait=not stopping, cancel_futures=stopping)
        return False


def compute_file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_task_summary(task: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "detection_mode": task["detection_mode"],
        "source_file_name": task["source_file_name"],
        "counts": task["counts"],
        "items": items,
    }


def _write_summary_csv(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "result_id",
        "task_id",
        "item_order",
        "source_ref",
        "status",
        "semantic_status",
        "top1_book_name",
        "top1_chapter_name",
        "top1_review_label",
        "top1_confidence_label",
        "top1_fine_score",
        "query_text_preview",
        "error_message",
        "created_at",
        "updated_at",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow({key: item.get(key, "") for key in fieldnames})


def _write_review_csv(path: Path, review_rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not review_rows:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "task_id",
                    "result_id",
                    "item_order",
                    "source_ref",
                    "book_name",
                    "chapter_name",
                    "fine_score",
                    "review_label",
                    "candidate_text_preview",
                ]
            )
        return
    fieldnames = list(review_rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in review_rows:
            writer.writerow(row)


def _write_result_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _enrich_review_rows(
    result_id: int,
    item_order: int,
    source_ref: str,
    review_rows: list[dict[str, Any]],
    task_id: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in review_rows:
        item = dict(row)
        item["task_id"] = task_id
        item["result_id"] = result_id
        item["item_order"] = item_order
        item["source_ref"] = source_ref
        enriched.append(item)
    return enriched


def _is_task_cancel_requested(task: dict[str, Any] | None) -> bool:
    if task is None:
        return False
    return str(task.get("status") or "") == "cancel_requested"


def _is_task_pause_requested(task: dict[str, Any] | None) -> bool:
    if task is None:
        return False
    return str(task.get("status") or "") == "pause_requested"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _mark_task_item_running(
    business_db_path: str | Path,
    task_id: str,
    item_order: int,
    status_message: str = "",
    touch_task_progress: bool = True,
    worker_lease_token: str | None = None,
) -> None:
    mark_task_items_running(
        db_path=business_db_path,
        task_id=task_id,
        item_orders=[int(item_order)],
        status_message=status_message,
        touch_task_progress=touch_task_progress,
        worker_lease_token=worker_lease_token,
    )


def _build_processing_status_message(
    submitted_count: int,
    total_count: int,
    running_count: int,
) -> str:
    if running_count > 1:
        return f"Processing item {submitted_count}/{total_count}. In flight {running_count}."
    return f"Processing item {submitted_count}/{total_count}."


def _build_paused_status_message(processed_count: int, total_count: int) -> str:
    return f"Task paused. Processed {processed_count}/{total_count} items."


def _should_persist_live_progress(last_persisted_at: float | None, now: float) -> bool:
    if last_persisted_at is None:
        return True
    return (now - last_persisted_at) >= TASK_PROGRESS_PERSIST_INTERVAL_SECONDS


def _build_live_processing_status_message(
    *,
    cancel_requested: bool,
    pause_requested: bool,
    processed_base_count: int,
    submitted_count: int,
    total_input_count: int,
    running_count: int,
) -> str:
    if cancel_requested or pause_requested or running_count <= 0:
        return ""
    progress_count = min(
        max(int(total_input_count or 0), 0),
        max(int(processed_base_count or 0), 0) + max(int(submitted_count or 0), 0),
    )
    return _build_processing_status_message(
        submitted_count=progress_count,
        total_count=total_input_count,
        running_count=running_count,
    )


def _sync_task_counts(
    business_db_path: str | Path,
    *,
    task_id: str,
    accepted_input_count: int,
    completed_input_count: int,
    failed_input_count: int,
    status_message: str = "",
    worker_lease_token: str | None = None,
) -> None:
    now = _now_iso()
    conn = connect_business_db(business_db_path)
    try:
        conn.execute(
            """
            UPDATE compare_tasks
               SET accepted_input_count = ?,
                   completed_input_count = ?,
                   failed_input_count = ?,
                   status_message = CASE
                       WHEN COALESCE(?, '') <> '' THEN ?
                       ELSE status_message
                   END,
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
               AND (? IS NULL OR worker_lease_token = ?)
            """,
            (
                int(accepted_input_count),
                int(completed_input_count),
                int(failed_input_count),
                status_message,
                status_message,
                now,
                now,
                task_id,
                worker_lease_token,
                worker_lease_token,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _run_compare_for_input_item(
    input_item: dict[str, Any],
    retrieval_db_path: str | Path,
    detection_mode: str,
    params: dict[str, Any],
    semantic_config: SemanticRetrievalConfig,
    compare_fn: CompareFn,
) -> TaskItemExecutionResult:
    payload = compare_fn(
        ComparePipelineRequest(
            db_path=str(retrieval_db_path),
            detection_mode=detection_mode,
            query_text=str(input_item["query_text"]),
            merged_top_k=params.get("merged_top_k"),
            compare_top_k=params.get("compare_top_k"),
            top_k=params.get("top_k"),
            candidate_display_score_threshold=params.get("candidate_display_score_threshold"),
            semantic_config=semantic_config,
        )
    )
    top1 = payload["fine"]["results"][0] if payload["fine"]["results"] else None
    return TaskItemExecutionResult(
        item_order=int(input_item["item_order"]),
        semantic_status=str(payload["rewrite_detection"]["status"]),
        top1_book_name="" if top1 is None else str(top1["book_name"]),
        top1_chapter_name="" if top1 is None else str(top1["chapter_name"]),
        top1_review_label="" if top1 is None else str(top1["review_label"]),
        top1_confidence_label="" if top1 is None else str(top1["confidence_label"]),
        top1_fine_score=None if top1 is None else float(top1["fine_score"]),
        result_payload=payload,
    )


def execute_claimed_task(
    task_id: str,
    business_db_path: str | Path,
    retrieval_db_path: str | Path,
    semantic_config: SemanticRetrievalConfig,
    export_root: str | Path,
    item_parallelism: int = 1,
    compare_fn: CompareFn = run_compare_pipeline,
    stop_event: threading.Event | None = None,
    worker_lease_token: str | None = None,
) -> dict[str, Any]:
    task = get_compare_task(business_db_path, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    if stop_event is not None and stop_event.is_set():
        settle_compare_task_for_worker_shutdown(
            business_db_path, task_id, worker_lease_token=worker_lease_token
        )
        return get_compare_task(business_db_path, task_id) or task

    source_file_path = Path(str(task["source_file_path"]))
    if not source_file_path.is_absolute():
        source_file_path = (ROOT_DIR / source_file_path).resolve()

    try:
        existing_item_count = count_compare_task_items(business_db_path, task_id)

        if not source_file_path.exists() and existing_item_count <= 0:
            finish_task(
                db_path=business_db_path,
                task_id=task_id,
                status="failed",
                status_message="Input file is missing and the task cannot be executed.",
                error_message=f"Input file not found: {source_file_path}",
                worker_lease_token=worker_lease_token,
            )
            return get_compare_task(business_db_path, task_id) or task

        parsed_inputs: list[dict[str, Any]] = []
        if source_file_path.exists():
            parsed_inputs = [
                {
                    "item_order": item.item_order,
                    "source_ref": item.source_ref,
                    "query_text": item.query_text,
                    "source_short_drama": item.source_short_drama,
                    "source_novel_name": item.source_novel_name,
                    "source_excel_row": item.source_excel_row,
                    "source_episode": item.source_episode,
                    "source_author": item.source_author,
                    "source_platform": item.source_platform,
                    "source_display_title": item.source_display_title,
                    "source_description": item.source_description,
                }
                for item in parse_task_input_file(source_file_path)
            ]
            if not parsed_inputs and existing_item_count <= 0:
                finish_task(
                    db_path=business_db_path,
                    task_id=task_id,
                    status="failed",
                    status_message="Input file contains no executable text.",
                    error_message="No valid query text found in uploaded file",
                    worker_lease_token=worker_lease_token,
                )
                return get_compare_task(business_db_path, task_id) or task

        processed_base_count = 0
        total_input_count = 0
        if existing_item_count <= 0:
            replace_task_items(
                db_path=business_db_path,
                task_id=task_id,
                items=parsed_inputs,
                worker_lease_token=worker_lease_token,
            )
            set_task_input_count(
                db_path=business_db_path,
                task_id=task_id,
                accepted_input_count=len(parsed_inputs),
                status_message=f"Parsed input file with {len(parsed_inputs)} pending text items.",
                worker_lease_token=worker_lease_token,
            )
            executable_inputs = list_compare_task_input_items(
                db_path=business_db_path,
                task_id=task_id,
            )
            total_input_count = len(executable_inputs)
        else:
            requeue_running_task_items(
                db_path=business_db_path,
                task_id=task_id,
                worker_lease_token=worker_lease_token,
            )
            existing_inputs = list_compare_task_input_items(
                db_path=business_db_path,
                task_id=task_id,
            )
            executable_inputs = [
                item
                for item in existing_inputs
                if str(item.get("status") or "") not in {"completed", "failed"}
            ]
            actual_completed_count = sum(
                1 for item in existing_inputs if str(item.get("status") or "") == "completed"
            )
            actual_failed_count = sum(
                1 for item in existing_inputs if str(item.get("status") or "") == "failed"
            )
            processed_base_count = actual_completed_count + actual_failed_count
            total_input_count = len(existing_inputs)
            _sync_task_counts(
                business_db_path,
                task_id=task_id,
                accepted_input_count=total_input_count,
                completed_input_count=actual_completed_count,
                failed_input_count=actual_failed_count,
                status_message=(
                    f"Parsed input file with {len(parsed_inputs)} pending text items."
                    if not executable_inputs
                    else (
                        f"Resumed task with {len(executable_inputs)} remaining text items."
                        if source_file_path.exists()
                        else f"Resumed task from stored inputs with {len(executable_inputs)} remaining text items."
                    )
                ),
                worker_lease_token=worker_lease_token,
            )

        params = dict(task["params"])
        task_export_dir = (Path(export_root) / task_id).resolve()
        task_export_dir.mkdir(parents=True, exist_ok=True)
        effective_parallelism = max(int(item_parallelism or 1), 1)

        all_review_rows: list[dict[str, Any]] = []
        next_input_index = 0
        submitted_count = 0
        cancel_requested = False
        pause_requested = False
        pending_completed_delta = 0
        pending_failed_delta = 0
        last_task_progress_persisted_at: float | None = None

        with _ShutdownAwareThreadPoolExecutor(
            max_workers=effective_parallelism,
            thread_name_prefix=f"task-{task_id[:8]}",
            stop_event=stop_event,
        ) as executor:
            in_flight: dict[Future[TaskItemExecutionResult], dict[str, Any]] = {}

            def flush_pending_task_progress(*, force: bool = False) -> None:
                nonlocal pending_completed_delta
                nonlocal pending_failed_delta
                nonlocal last_task_progress_persisted_at
                has_pending_counts = pending_completed_delta > 0 or pending_failed_delta > 0
                status_message = _build_live_processing_status_message(
                    cancel_requested=cancel_requested,
                    pause_requested=pause_requested,
                    processed_base_count=processed_base_count,
                    submitted_count=submitted_count,
                    total_input_count=total_input_count,
                    running_count=len(in_flight),
                )
                if not has_pending_counts and not status_message:
                    return
                now_monotonic = time.monotonic()
                if (
                    not force
                    and not has_pending_counts
                    and not _should_persist_live_progress(last_task_progress_persisted_at, now_monotonic)
                ):
                    return
                if (
                    not force
                    and has_pending_counts
                    and not _should_persist_live_progress(last_task_progress_persisted_at, now_monotonic)
                ):
                    return
                update_task_progress(
                    db_path=business_db_path,
                    task_id=task_id,
                    completed_delta=pending_completed_delta,
                    failed_delta=pending_failed_delta,
                    status_message=status_message,
                    worker_lease_token=worker_lease_token,
                )
                last_task_progress_persisted_at = now_monotonic
                pending_completed_delta = 0
                pending_failed_delta = 0

            def submit_input_item(input_item: dict[str, Any]) -> None:
                in_flight[
                    executor.submit(
                        _run_compare_for_input_item,
                        input_item=input_item,
                        retrieval_db_path=retrieval_db_path,
                        detection_mode=str(task["detection_mode"]),
                        params=params,
                        semantic_config=semantic_config,
                        compare_fn=compare_fn,
                    )
                ] = input_item

            def submit_input_batch(input_batch: list[dict[str, Any]]) -> None:
                nonlocal submitted_count
                nonlocal last_task_progress_persisted_at
                if not input_batch:
                    return
                submitted_count += len(input_batch)
                status_message = _build_processing_status_message(
                    submitted_count=min(
                        total_input_count,
                        processed_base_count + submitted_count,
                    ),
                    total_count=total_input_count,
                    running_count=len(in_flight) + len(input_batch),
                )
                now_monotonic = time.monotonic()
                should_touch_task_progress = _should_persist_live_progress(
                    last_task_progress_persisted_at,
                    now_monotonic,
                )
                mark_task_items_running(
                    db_path=business_db_path,
                    task_id=task_id,
                    item_orders=[int(item["item_order"]) for item in input_batch],
                    status_message=status_message if should_touch_task_progress else "",
                    touch_task_progress=should_touch_task_progress,
                    worker_lease_token=worker_lease_token,
                )
                if should_touch_task_progress:
                    last_task_progress_persisted_at = now_monotonic
                for input_item in input_batch:
                    submit_input_item(input_item)

            while next_input_index < len(executable_inputs) and len(in_flight) < effective_parallelism:
                if stop_event is not None and stop_event.is_set():
                    break
                current_task = get_compare_task_runtime_state(business_db_path, task_id)
                if _is_task_cancel_requested(current_task):
                    cancel_requested = True
                    break
                if _is_task_pause_requested(current_task):
                    pause_requested = True
                    break
                initial_batch = executable_inputs[
                    next_input_index : next_input_index + (effective_parallelism - len(in_flight))
                ]
                submit_input_batch(initial_batch)
                next_input_index += len(initial_batch)

            while in_flight:
                if stop_event is None:
                    done, _ = wait(
                        set(in_flight.keys()),
                        return_when=FIRST_COMPLETED,
                    )
                else:
                    done, _ = wait(
                        set(in_flight.keys()),
                        timeout=0.25,
                        return_when=FIRST_COMPLETED,
                    )
                if not done:
                    if stop_event is not None and stop_event.is_set():
                        break
                    continue
                completed_successes: list[dict[str, Any]] = []
                completed_failures: list[dict[str, Any]] = []
                for future in done:
                    input_item = in_flight.pop(future)
                    item_order = int(input_item["item_order"])
                    try:
                        result = future.result()
                        completed_successes.append(
                            {
                                "item_order": result.item_order,
                                "semantic_status": result.semantic_status,
                                "top1_book_name": result.top1_book_name,
                                "top1_chapter_name": result.top1_chapter_name,
                                "top1_review_label": result.top1_review_label,
                                "top1_confidence_label": result.top1_confidence_label,
                                "top1_fine_score": result.top1_fine_score,
                                "result_payload": result.result_payload,
                                "payload_result_id": int(input_item.get("result_id") or 0),
                                "payload_query_text": str(input_item.get("query_text") or ""),
                                "payload_created_at": str(input_item.get("created_at") or ""),
                            }
                        )
                        pending_completed_delta += 1
                    except Exception as exc:
                        completed_failures.append(
                            {
                                "item_order": item_order,
                                "error_message": str(exc),
                            }
                        )
                        pending_failed_delta += 1
                save_task_item_outcomes_batch(
                    db_path=business_db_path,
                    task_id=task_id,
                    successes=completed_successes,
                    failures=completed_failures,
                    update_task_counts=False,
                    worker_lease_token=worker_lease_token,
                )

                current_task = get_compare_task_runtime_state(business_db_path, task_id)
                if _is_task_cancel_requested(current_task):
                    cancel_requested = True
                if _is_task_pause_requested(current_task):
                    pause_requested = True

                # Flush completed/failed counters before refilling more work.
                # Otherwise a submit-side heartbeat touch can keep resetting the
                # shared throttle window and starve live progress persistence.
                flush_pending_task_progress()

                while (
                    not cancel_requested
                    and not pause_requested
                    and next_input_index < len(executable_inputs)
                    and len(in_flight) < effective_parallelism
                ):
                    refill_batch = executable_inputs[
                        next_input_index : next_input_index + (effective_parallelism - len(in_flight))
                    ]
                    submit_input_batch(refill_batch)
                    next_input_index += len(refill_batch)

                flush_pending_task_progress(
                    force=cancel_requested or pause_requested or not in_flight,
                )

                if stop_event is not None and stop_event.is_set():
                    flush_pending_task_progress(force=True)
                    break

            if cancel_requested:
                finish_task(
                    db_path=business_db_path,
                    task_id=task_id,
                    status="cancelled",
                    status_message="Task cancelled during execution.",
                    worker_lease_token=worker_lease_token,
                )
                return get_compare_task(business_db_path, task_id) or task

            if pause_requested:
                refreshed = get_compare_task_runtime_state(business_db_path, task_id)
                processed_count = 0 if refreshed is None else int(
                    (refreshed.get("counts") or {}).get("completed", 0)
                ) + int((refreshed.get("counts") or {}).get("failed", 0))
                mark_task_paused(
                    db_path=business_db_path,
                    task_id=task_id,
                    status_message=_build_paused_status_message(
                        processed_count=processed_count,
                        total_count=total_input_count,
                    ),
                    worker_lease_token=worker_lease_token,
                )
                return get_compare_task(business_db_path, task_id) or task

        if stop_event is not None and stop_event.is_set():
            settle_compare_task_for_worker_shutdown(
                business_db_path, task_id, worker_lease_token=worker_lease_token
            )
            return get_compare_task(business_db_path, task_id) or task

        refreshed_task = get_compare_task(business_db_path, task_id)
        if refreshed_task is None:
            raise ValueError(f"Task disappeared after execution: {task_id}")
        items = list_compare_task_items(
            db_path=business_db_path,
            task_id=task_id,
            limit=max(int(refreshed_task["counts"]["accepted"] or 0), 1),
            offset=0,
        )

        for payload_item in list_compare_task_result_payloads(
            business_db_path,
            task_id,
        ):
            all_review_rows.extend(
                _enrich_review_rows(
                    result_id=int(payload_item["result_id"]),
                    item_order=int(payload_item["item_order"]),
                    source_ref=str(payload_item["source_ref"]),
                    review_rows=list(
                        dict(payload_item.get("result_payload") or {})
                        .get("fine", {})
                        .get("review_rows", [])
                    ),
                    task_id=task_id,
                )
            )

        summary_csv_path = task_export_dir / "task_summary.csv"
        review_csv_path = task_export_dir / "task_review_rows.csv"
        result_json_path = task_export_dir / "task_summary.json"
        _write_summary_csv(summary_csv_path, items=items)
        _write_review_csv(review_csv_path, review_rows=all_review_rows)
        _write_result_json(
            result_json_path,
            payload=build_task_summary(refreshed_task, items=items),
        )

        completed = int(refreshed_task["counts"]["completed"])
        failed = int(refreshed_task["counts"]["failed"])
        accepted = int(refreshed_task["counts"]["accepted"] or 0)
        if completed == 0 and failed > 0:
            final_status = "failed"
            message = f"Task failed. {failed}/{accepted} items ended in error."
        elif failed > 0:
            final_status = "partial_failed"
            message = f"Task completed with partial failure. {failed}/{accepted} items failed."
        else:
            final_status = "completed"
            message = f"Task completed successfully. Processed {completed} items."

        # Persist the same final task status in the exported JSON summary.
        final_summary_task = dict(refreshed_task)
        final_summary_task["status"] = final_status

        _write_result_json(
            result_json_path,
            payload=build_task_summary(final_summary_task, items=items),
        )

        finish_task(
            db_path=business_db_path,
            task_id=task_id,
            status=final_status,
            status_message=message,
            summary_export_path=str(summary_csv_path),
            review_export_path=str(review_csv_path),
            result_json_path=str(result_json_path),
            worker_lease_token=worker_lease_token,
        )
        return get_compare_task(business_db_path, task_id) or refreshed_task
    except Exception as exc:
        finish_task(
            db_path=business_db_path,
            task_id=task_id,
            status="failed",
            status_message="Task execution failed with an unhandled exception.",
            error_message=str(exc),
            worker_lease_token=worker_lease_token,
        )
        return get_compare_task(business_db_path, task_id) or task


def run_next_queued_task(
    business_db_path: str | Path,
    retrieval_db_path: str | Path,
    semantic_config: SemanticRetrievalConfig,
    export_root: str | Path,
    worker_name: str,
    item_parallelism: int = 1,
    task_recovery_stale_seconds: float = DEFAULT_TASK_RECOVERY_STALE_SECONDS,
    compare_fn: CompareFn = run_compare_pipeline,
) -> dict[str, Any] | None:
    claimed_task = claim_next_compare_task(
        db_path=business_db_path,
        worker_name=worker_name,
        stale_after_seconds=task_recovery_stale_seconds,
    )
    if claimed_task is None:
        return None
    return execute_claimed_task(
        task_id=str(claimed_task["task_id"]),
        business_db_path=business_db_path,
        retrieval_db_path=retrieval_db_path,
        semantic_config=semantic_config,
        export_root=export_root,
        item_parallelism=item_parallelism,
        compare_fn=compare_fn,
        worker_lease_token=str(claimed_task.get("_worker_lease_token") or "") or None,
    )
