from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from hashlib import sha256
from datetime import datetime
from pathlib import Path
import csv
import json
from dataclasses import dataclass
from typing import Any, Callable

from service.business_store import (
    claim_next_compare_task,
    count_compare_task_items,
    connect_business_db,
    finish_task,
    get_compare_result,
    get_compare_task,
    list_compare_task_input_items,
    list_compare_task_items,
    mark_task_heartbeat,
    mark_task_paused,
    requeue_running_task_items,
    replace_task_items,
    save_task_item_failure,
    save_task_item_success,
    set_task_input_count,
)
from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline
from service.semantic_retrieval import SemanticRetrievalConfig
from service.task_input_parser import parse_task_input_file


ROOT_DIR = Path(__file__).resolve().parent.parent
CompareFn = Callable[[ComparePipelineRequest], dict[str, Any]]


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
) -> None:
    now = _now_iso()
    conn = connect_business_db(business_db_path)
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET started_at = COALESCE(started_at, ?),
                   updated_at = ?,
                   status = 'running'
             WHERE task_id = ?
               AND item_order = ?
            """,
            (now, now, task_id, int(item_order)),
        )
        conn.execute(
            """
            UPDATE compare_tasks
               SET updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()


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
) -> dict[str, Any]:
    task = get_compare_task(business_db_path, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    source_file_path = Path(str(task["source_file_path"]))
    if not source_file_path.is_absolute():
        source_file_path = (ROOT_DIR / source_file_path).resolve()

    try:
        if not source_file_path.exists():
            finish_task(
                db_path=business_db_path,
                task_id=task_id,
                status="failed",
                status_message="Input file is missing and the task cannot be executed.",
                error_message=f"Input file not found: {source_file_path}",
            )
            return get_compare_task(business_db_path, task_id) or task

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
        if not parsed_inputs:
            finish_task(
                db_path=business_db_path,
                task_id=task_id,
                status="failed",
                status_message="Input file contains no executable text.",
                error_message="No valid query text found in uploaded file",
            )
            return get_compare_task(business_db_path, task_id) or task

        existing_item_count = count_compare_task_items(business_db_path, task_id)
        if existing_item_count <= 0:
            replace_task_items(
                db_path=business_db_path,
                task_id=task_id,
                items=parsed_inputs,
            )
            set_task_input_count(
                db_path=business_db_path,
                task_id=task_id,
                accepted_input_count=len(parsed_inputs),
                status_message=f"Parsed input file with {len(parsed_inputs)} pending text items.",
            )
            executable_inputs = parsed_inputs
        else:
            requeue_running_task_items(
                db_path=business_db_path,
                task_id=task_id,
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
            set_task_input_count(
                db_path=business_db_path,
                task_id=task_id,
                accepted_input_count=len(parsed_inputs),
                status_message=(
                    f"Parsed input file with {len(parsed_inputs)} pending text items."
                    if not executable_inputs
                    else f"Resumed task with {len(executable_inputs)} remaining text items."
                ),
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
        total_input_count = len(parsed_inputs)

        with ThreadPoolExecutor(
            max_workers=effective_parallelism,
            thread_name_prefix=f"task-{task_id[:8]}",
        ) as executor:
            in_flight: dict[Future[TaskItemExecutionResult], dict[str, Any]] = {}

            def submit_input_item(input_item: dict[str, Any]) -> None:
                nonlocal submitted_count
                item_order = int(input_item["item_order"])
                submitted_count += 1
                _mark_task_item_running(
                    business_db_path=business_db_path,
                    task_id=task_id,
                    item_order=item_order,
                )
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
                mark_task_heartbeat(
                    db_path=business_db_path,
                    task_id=task_id,
                    status_message=_build_processing_status_message(
                        submitted_count=submitted_count,
                        total_count=total_input_count,
                        running_count=len(in_flight),
                    ),
                )

            while next_input_index < len(executable_inputs) and len(in_flight) < effective_parallelism:
                current_task = get_compare_task(business_db_path, task_id)
                if _is_task_cancel_requested(current_task):
                    cancel_requested = True
                    break
                if _is_task_pause_requested(current_task):
                    pause_requested = True
                    break
                submit_input_item(executable_inputs[next_input_index])
                next_input_index += 1

            while in_flight:
                done, _ = wait(set(in_flight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    input_item = in_flight.pop(future)
                    item_order = int(input_item["item_order"])
                    try:
                        result = future.result()
                        save_task_item_success(
                            db_path=business_db_path,
                            task_id=task_id,
                            item_order=result.item_order,
                            semantic_status=result.semantic_status,
                            top1_book_name=result.top1_book_name,
                            top1_chapter_name=result.top1_chapter_name,
                            top1_review_label=result.top1_review_label,
                            top1_confidence_label=result.top1_confidence_label,
                            top1_fine_score=result.top1_fine_score,
                            result_payload=result.result_payload,
                        )
                    except Exception as exc:
                        save_task_item_failure(
                            db_path=business_db_path,
                            task_id=task_id,
                            item_order=item_order,
                            error_message=str(exc),
                        )

                current_task = get_compare_task(business_db_path, task_id)
                if _is_task_cancel_requested(current_task):
                    cancel_requested = True
                if _is_task_pause_requested(current_task):
                    pause_requested = True

                while (
                    not cancel_requested
                    and not pause_requested
                    and next_input_index < len(executable_inputs)
                    and len(in_flight) < effective_parallelism
                ):
                    submit_input_item(executable_inputs[next_input_index])
                    next_input_index += 1

            if cancel_requested:
                finish_task(
                    db_path=business_db_path,
                    task_id=task_id,
                    status="cancelled",
                    status_message="Task cancelled during execution.",
                )
                return get_compare_task(business_db_path, task_id) or task

            if pause_requested:
                refreshed = get_compare_task(business_db_path, task_id)
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

        for item in items:
            if item["status"] != "completed":
                continue
            result_detail = get_compare_result(business_db_path, int(item["result_id"]))
            if result_detail is None:
                continue
            all_review_rows.extend(
                _enrich_review_rows(
                    result_id=int(item["result_id"]),
                    item_order=int(item["item_order"]),
                    source_ref=str(item["source_ref"]),
                    review_rows=list(result_detail["result_payload"].get("fine", {}).get("review_rows", [])),
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
        )
        return get_compare_task(business_db_path, task_id) or refreshed_task
    except Exception as exc:
        finish_task(
            db_path=business_db_path,
            task_id=task_id,
            status="failed",
            status_message="Task execution failed with an unhandled exception.",
            error_message=str(exc),
        )
        return get_compare_task(business_db_path, task_id) or task


def run_next_queued_task(
    business_db_path: str | Path,
    retrieval_db_path: str | Path,
    semantic_config: SemanticRetrievalConfig,
    export_root: str | Path,
    worker_name: str,
    item_parallelism: int = 1,
    compare_fn: CompareFn = run_compare_pipeline,
) -> dict[str, Any] | None:
    claimed_task = claim_next_compare_task(
        db_path=business_db_path,
        worker_name=worker_name,
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
    )
