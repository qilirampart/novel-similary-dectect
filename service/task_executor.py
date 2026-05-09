from __future__ import annotations

from hashlib import sha256
from datetime import datetime
from pathlib import Path
import csv
import json
from typing import Any, Callable

from service.business_store import (
    claim_next_compare_task,
    finish_task,
    get_compare_result,
    get_compare_task,
    list_compare_task_items,
    mark_task_heartbeat,
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


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def execute_claimed_task(
    task_id: str,
    business_db_path: str | Path,
    retrieval_db_path: str | Path,
    semantic_config: SemanticRetrievalConfig,
    export_root: str | Path,
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

        params = dict(task["params"])
        task_export_dir = (Path(export_root) / task_id).resolve()
        task_export_dir.mkdir(parents=True, exist_ok=True)

        all_review_rows: list[dict[str, Any]] = []
        for input_item in parsed_inputs:
            current_task = get_compare_task(business_db_path, task_id)
            if _is_task_cancel_requested(current_task):
                finish_task(
                    db_path=business_db_path,
                    task_id=task_id,
                    status="cancelled",
                    status_message="Task cancelled during execution.",
                )
                return get_compare_task(business_db_path, task_id) or task

            item_order = int(input_item["item_order"])
            source_ref = str(input_item.get("source_ref", ""))
            now = _now_iso()
            conn = None
            mark_task_heartbeat(
                db_path=business_db_path,
                task_id=task_id,
                status_message=f"Processing item {item_order}/{len(parsed_inputs)}.",
            )
            try:
                from service.business_store import connect_business_db

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
                        (now, now, task_id, item_order),
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
                payload = compare_fn(
                    ComparePipelineRequest(
                        db_path=str(retrieval_db_path),
                        detection_mode=task["detection_mode"],
                        query_text=str(input_item["query_text"]),
                        merged_top_k=params.get("merged_top_k"),
                        compare_top_k=params.get("compare_top_k"),
                        top_k=params.get("top_k"),
                        candidate_display_score_threshold=params.get("candidate_display_score_threshold"),
                        semantic_config=semantic_config,
                    )
                )
                top1 = payload["fine"]["results"][0] if payload["fine"]["results"] else None
                save_task_item_success(
                    db_path=business_db_path,
                    task_id=task_id,
                    item_order=item_order,
                    semantic_status=str(payload["rewrite_detection"]["status"]),
                    top1_book_name="" if top1 is None else str(top1["book_name"]),
                    top1_chapter_name="" if top1 is None else str(top1["chapter_name"]),
                    top1_review_label="" if top1 is None else str(top1["review_label"]),
                    top1_confidence_label="" if top1 is None else str(top1["confidence_label"]),
                    top1_fine_score=None if top1 is None else float(top1["fine_score"]),
                    result_payload=payload,
                )
            except Exception as exc:
                save_task_item_failure(
                    db_path=business_db_path,
                    task_id=task_id,
                    item_order=item_order,
                    error_message=str(exc),
                )

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
        compare_fn=compare_fn,
    )
