from __future__ import annotations

from pathlib import Path
import time

from service.business_store import (
    claim_next_queued_task_globally,
    connect_business_db,
    create_compare_task,
    finish_task,
    get_business_db_lock_metrics,
    get_compare_task,
    get_task_queue_metadata,
    init_business_db,
    list_compare_task_items,
    mark_task_items_running,
    recover_interrupted_tasks,
    reset_business_db_lock_metrics,
    replace_task_items,
    save_task_item_outcomes_batch,
    set_task_input_count,
)
from service.drama_subtitle_task_store import (
    create_drama_subtitle_task,
    finish_drama_subtitle_task,
    get_drama_subtitle_task,
    list_drama_subtitle_task_items,
    recover_interrupted_drama_subtitle_tasks,
    replace_drama_subtitle_task_items,
    save_drama_subtitle_task_item_outcome,
)


def _create_compare_task(db_path: Path, task_id: str, accepted_input_count: int) -> None:
    create_compare_task(
        db_path=db_path,
        task_id=task_id,
        detection_mode="rewrite",
        source_file_name=f"{task_id}.txt",
        source_file_ext=".txt",
        source_file_path=f"runtime/{task_id}.txt",
        source_file_sha256="test",
        source_file_size=1,
        params={},
        accepted_input_count=accepted_input_count,
    )


def _create_subtitle_task(db_path: Path, task_id: str, accepted_input_count: int) -> None:
    create_drama_subtitle_task(
        db_path=db_path,
        task_id=task_id,
        source_file_name=f"{task_id}.xlsx",
        source_file_ext=".xlsx",
        source_file_path=f"runtime/{task_id}.xlsx",
        source_file_sha256="test",
        source_file_size=1,
        owner_user_id=None,
        created_by="pytest",
        accepted_input_count=accepted_input_count,
    )


def test_init_business_db_migrates_existing_task_tables_with_worker_lease(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-business.sqlite3"
    schema_path = Path(__file__).resolve().parent.parent / "service" / "business_schema_v1.sql"
    legacy_schema = schema_path.read_text(encoding="utf-8").replace(
        "    worker_lease_token TEXT,\n",
        "",
    )
    conn = connect_business_db(db_path)
    try:
        conn.executescript(legacy_schema)
        conn.commit()
    finally:
        conn.close()

    init_business_db(db_path)
    conn = connect_business_db(db_path)
    try:
        compare_columns = {row[1] for row in conn.execute("PRAGMA table_info(compare_tasks)")}
        subtitle_columns = {row[1] for row in conn.execute("PRAGMA table_info(drama_subtitle_tasks)")}
    finally:
        conn.close()
    assert "worker_lease_token" in compare_columns
    assert "worker_lease_token" in subtitle_columns


def test_global_dispatch_claims_oldest_task_and_reports_queue_wait(tmp_path: Path) -> None:
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    _create_compare_task(db_path, "compare-old", accepted_input_count=2)
    _create_subtitle_task(db_path, "subtitle-new", accepted_input_count=4)

    first_claim = claim_next_queued_task_globally(db_path, worker_name="worker-1")
    assert first_claim is not None
    assert first_claim["task_kind"] == "compare"
    assert first_claim["task_id"] == "compare-old"
    assert len(first_claim["worker_lease_token"]) == 32

    metadata = get_task_queue_metadata(
        db_path,
        task_kind="drama_subtitle",
        task_id="subtitle-new",
        worker_count=2,
        seconds_per_item=5,
    )
    assert metadata["state"] == "queued"
    assert metadata["queue_position"] == 1
    assert metadata["tasks_ahead_count"] == 1
    assert metadata["items_ahead_count"] == 2
    assert metadata["estimated_wait_seconds"] == 5

    second_claim = claim_next_queued_task_globally(db_path, worker_name="worker-2")
    assert second_claim is not None
    assert second_claim["task_kind"] == "drama_subtitle"
    assert second_claim["task_id"] == "subtitle-new"
    assert second_claim["worker_lease_token"] != first_claim["worker_lease_token"]


def test_background_claim_and_recovery_do_not_stall_on_sqlite_writer_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    _create_compare_task(db_path, "compare-locked", accepted_input_count=1)
    _create_compare_task(db_path, "compare-stale", accepted_input_count=1)
    stale_conn = connect_business_db(db_path)
    try:
        stale_conn.execute(
            """
            UPDATE compare_tasks
               SET status = 'running', last_heartbeat_at = '2000-01-01T00:00:00'
             WHERE task_id = 'compare-stale'
            """
        )
        stale_conn.commit()
    finally:
        stale_conn.close()
    reset_business_db_lock_metrics()

    blocker = connect_business_db(db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")

        claim_started = time.monotonic()
        claim = claim_next_queued_task_globally(db_path, worker_name="worker-contended")
        claim_elapsed = time.monotonic() - claim_started
        assert claim is None
        assert claim_elapsed < 1.5

        recovery_started = time.monotonic()
        recovery = recover_interrupted_tasks(db_path, stale_after_seconds=1)
        recovery_elapsed = time.monotonic() - recovery_started
        assert recovery["running_to_queued"] == 0
        assert recovery_elapsed < 1.5
    finally:
        blocker.rollback()
        blocker.close()

    claimed = claim_next_queued_task_globally(db_path, worker_name="worker-after-lock")
    assert claimed is not None
    assert claimed["task_id"] == "compare-locked"

    metrics = get_business_db_lock_metrics()
    assert metrics["global_task_claim"]["lock_events"] >= 3
    assert metrics["global_task_claim"]["skipped"] == 1
    assert metrics["compare_recovery"]["lock_events"] >= 3
    assert metrics["compare_recovery"]["skipped"] == 1


def test_idle_claim_and_recovery_do_not_open_write_transactions(tmp_path: Path) -> None:
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    reset_business_db_lock_metrics()

    assert claim_next_queued_task_globally(db_path, worker_name="idle-worker") is None
    recovery = recover_interrupted_tasks(db_path, stale_after_seconds=1)

    assert recovery["running_to_queued"] == 0
    assert get_business_db_lock_metrics() == {}


def test_stale_subtitle_task_returns_to_queue(tmp_path: Path) -> None:
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    _create_subtitle_task(db_path, "subtitle-stale", accepted_input_count=3)
    first_claim = claim_next_queued_task_globally(db_path, worker_name="worker-1")
    assert first_claim is not None
    assert first_claim["task_kind"] == "drama_subtitle"
    assert first_claim["task_id"] == "subtitle-stale"

    conn = connect_business_db(db_path)
    try:
        conn.execute(
            "UPDATE drama_subtitle_tasks SET last_heartbeat_at = '2000-01-01T00:00:00' WHERE task_id = ?",
            ("subtitle-stale",),
        )
        conn.commit()
    finally:
        conn.close()

    summary = recover_interrupted_drama_subtitle_tasks(db_path, stale_after_seconds=10)
    assert summary["running_to_queued"] == 1
    next_claim = claim_next_queued_task_globally(db_path, worker_name="worker-2")
    assert next_claim is not None
    assert next_claim["task_kind"] == "drama_subtitle"
    assert next_claim["task_id"] == "subtitle-stale"
    assert next_claim["worker_lease_token"] != first_claim["worker_lease_token"]


def test_compare_worker_lease_rejects_late_result_after_recovery(tmp_path: Path) -> None:
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    _create_compare_task(db_path, "compare-lease", accepted_input_count=1)
    replace_task_items(
        db_path,
        "compare-lease",
        [{"item_order": 1, "source_ref": "1", "query_text": "lease test"}],
    )
    set_task_input_count(db_path, "compare-lease", 1)

    claim_a = claim_next_queued_task_globally(db_path, worker_name="worker-a")
    assert claim_a is not None
    token_a = claim_a["worker_lease_token"]
    mark_task_items_running(
        db_path,
        "compare-lease",
        [1],
        worker_lease_token=token_a,
    )
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            "UPDATE compare_tasks SET last_heartbeat_at = '2000-01-01T00:00:00' WHERE task_id = ?",
            ("compare-lease",),
        )
        conn.commit()
    finally:
        conn.close()

    assert recover_interrupted_tasks(db_path, stale_after_seconds=1)["running_to_queued"] == 1
    claim_b = claim_next_queued_task_globally(db_path, worker_name="worker-b")
    assert claim_b is not None
    token_b = claim_b["worker_lease_token"]
    assert token_b != token_a

    stale_success = {
        "item_order": 1,
        "semantic_status": "stale",
        "top1_book_name": "stale-result",
        "top1_chapter_name": "",
        "top1_review_label": "",
        "top1_confidence_label": "",
        "top1_fine_score": 0.99,
        "result_payload": {"source": "worker-a"},
    }
    save_task_item_outcomes_batch(
        db_path,
        "compare-lease",
        successes=[stale_success],
        worker_lease_token=token_a,
    )
    finish_task(
        db_path,
        "compare-lease",
        "completed",
        "stale worker completed",
        worker_lease_token=token_a,
    )

    task = get_compare_task(db_path, "compare-lease")
    item = list_compare_task_items(db_path, "compare-lease", limit=10)[0]
    assert task is not None and task["status"] == "running"
    assert task["counts"]["completed"] == 0
    assert item["status"] == "queued"
    assert item["top1_book_name"] == ""

    fresh_success = {**stale_success, "semantic_status": "fresh", "top1_book_name": "fresh-result"}
    save_task_item_outcomes_batch(
        db_path,
        "compare-lease",
        successes=[fresh_success],
        worker_lease_token=token_b,
    )
    finish_task(
        db_path,
        "compare-lease",
        "completed",
        "fresh worker completed",
        worker_lease_token=token_b,
    )
    task = get_compare_task(db_path, "compare-lease")
    item = list_compare_task_items(db_path, "compare-lease", limit=10)[0]
    assert task is not None and task["status"] == "completed"
    assert task["counts"]["completed"] == 1
    assert item["top1_book_name"] == "fresh-result"


def test_subtitle_worker_lease_rejects_late_result_after_recovery(tmp_path: Path) -> None:
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    _create_subtitle_task(db_path, "subtitle-lease", accepted_input_count=1)
    replace_drama_subtitle_task_items(
        db_path=db_path,
        task_id="subtitle-lease",
        items=[{"item_order": 1, "source_ref": "1", "query_text": "lease test"}],
    )

    claim_a = claim_next_queued_task_globally(db_path, worker_name="worker-a")
    assert claim_a is not None
    token_a = claim_a["worker_lease_token"]
    conn = connect_business_db(db_path)
    try:
        conn.execute(
            "UPDATE drama_subtitle_tasks SET last_heartbeat_at = '2000-01-01T00:00:00' WHERE task_id = ?",
            ("subtitle-lease",),
        )
        conn.commit()
    finally:
        conn.close()

    assert recover_interrupted_drama_subtitle_tasks(db_path, stale_after_seconds=1)["running_to_queued"] == 1
    claim_b = claim_next_queued_task_globally(db_path, worker_name="worker-b")
    assert claim_b is not None
    token_b = claim_b["worker_lease_token"]
    assert token_b != token_a

    def save_outcome(token: str, book_name: str) -> None:
        save_drama_subtitle_task_item_outcome(
            db_path=db_path,
            task_id="subtitle-lease",
            item_order=1,
            status="completed",
            duration_seconds=1.0,
            query_language_code="en",
            query_language_confidence=1.0,
            result_payload={
                "decision": {"status": "matched", "matched": True, "candidate_rank": 1},
                "candidates": [{"rank": 1, "book_id": book_name, "book_name": book_name}],
            },
            worker_lease_token=token,
        )

    save_outcome(token_a, "stale-result")
    finish_drama_subtitle_task(
        db_path=db_path,
        task_id="subtitle-lease",
        status="completed",
        status_message="stale worker completed",
        worker_lease_token=token_a,
    )
    task = get_drama_subtitle_task(db_path, "subtitle-lease")
    item = list_drama_subtitle_task_items(db_path, "subtitle-lease")[0]
    assert task is not None and task["status"] == "running"
    assert task["counts"]["completed"] == 0
    assert item["status"] == "queued"
    assert item["matched_book_name"] in {None, ""}

    save_outcome(token_b, "fresh-result")
    finish_drama_subtitle_task(
        db_path=db_path,
        task_id="subtitle-lease",
        status="completed",
        status_message="fresh worker completed",
        worker_lease_token=token_b,
    )
    task = get_drama_subtitle_task(db_path, "subtitle-lease")
    item = list_drama_subtitle_task_items(db_path, "subtitle-lease")[0]
    assert task is not None and task["status"] == "completed"
    assert task["counts"]["completed"] == 1
    assert item["matched_book_name"] == "fresh-result"
