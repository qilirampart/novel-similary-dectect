from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.business_store import (
    claim_next_compare_task,
    clear_pending_review_results,
    count_compare_results,
    delete_compare_task,
    _enrich_result_payload_for_review,
    create_compare_task,
    get_compare_result,
    get_compare_task,
    init_business_db,
    list_compare_results,
    list_compare_task_items,
    list_compare_tasks,
    pause_compare_task,
    recover_interrupted_tasks,
    replace_task_items,
    resume_compare_task,
    set_task_input_count,
    summarize_compare_results,
    upsert_compare_task_review,
)
from service.semantic_retrieval import SemanticRetrievalConfig
from service.task_executor import execute_claimed_task


def _fake_compare_pipeline(request):  # type: ignore[no-untyped-def]
    observed_thresholds.append(request.candidate_display_score_threshold)
    return {
        "query_text": request.query_text,
        "detection_mode": request.detection_mode,
        "detection_mode_label": "测试",
        "detection_mode_description": "测试用",
        "params": {
            "candidate_display_score_threshold": request.candidate_display_score_threshold,
        },
        "capabilities": {
            "reuse_detection_enabled": True,
            "rewrite_detection_enabled": True,
            "semantic_recall_enabled": False,
        },
        "coarse": {
            "target_count": 2,
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
                    "book_name": "测试小说",
                    "chapter_name": "第1章",
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
            "message": "测试降级",
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
                    "book_name": "测试小说",
                    "chapter_name": "第1章",
                    "review_label": "强证据",
                    "confidence_label": "strong",
                    "fine_score": 0.91,
                }
            ],
            "review_rows": [
                {
                    "detection_mode": request.detection_mode,
                    "fine_rank": 1,
                    "review_label": "强证据",
                    "confidence_label": "strong",
                    "fine_score": 0.91,
                    "coarse_rank": 1,
                    "coarse_final_score": 0.81,
                    "dataset_key": "self_short_novels",
                    "book_ext_id": "book-1",
                    "book_name": "测试小说",
                    "chapter_uid": 1,
                    "chapter_ext_id": 1,
                    "chapter_name": "第1章",
                    "candidate_window_order": 1,
                    "candidate_start_offset": 0,
                    "candidate_end_offset": 200,
                    "exact_substring_hit": True,
                    "longest_match_len": 120,
                    "longest_match_ratio": 1.0,
                    "ngram_recall": 1.0,
                    "ngram_precision": 1.0,
                    "jaccard": 1.0,
                    "sequence_ratio": 1.0,
                    "matched_substring": request.query_text,
                    "query_text_preview": request.query_text[:30],
                    "candidate_text_preview": request.query_text[:30],
                    "query_text": request.query_text,
                    "candidate_text": request.query_text,
                }
            ],
        },
    }

observed_thresholds: list[float | None] = []


def _scored_compare_pipeline(request):  # type: ignore[no-untyped-def]
    payload = json.loads(json.dumps(_fake_compare_pipeline(request), ensure_ascii=False))
    score = 0.15
    for keyword, value in (
        ("第一", 0.41),
        ("第二", 0.83),
        ("第三", 0.67),
        ("第四", 0.52),
    ):
        if keyword in request.query_text:
            score = value
            break

    payload["reuse_detection"]["results"][0]["fine_score"] = score
    payload["fine"]["results"][0]["fine_score"] = score
    payload["fine"]["review_rows"][0]["fine_score"] = score
    return payload


def test_batch_task_smoke(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batch.txt"
    input_path.write_text("第一条测试文案\n\n第二条测试文案", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-smoke-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256",
        source_file_size=input_path.stat().st_size,
        params={
            "top_k": 3,
            "compare_top_k": 5,
            "merged_top_k": 8,
            "candidate_display_score_threshold": 0.35,
        },
        created_by="pytest",
    )
    assert task["status"] == "queued"
    assert task["params"]["candidate_display_score_threshold"] == 0.35

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports",
        compare_fn=_fake_compare_pipeline,
    )

    assert finished["status"] == "completed"
    assert finished["params"]["candidate_display_score_threshold"] == 0.35
    assert finished["counts"]["accepted"] == 2
    assert finished["counts"]["completed"] == 2
    assert finished["counts"]["failed"] == 0
    assert finished["summary_export_path"]
    assert finished["review_export_path"]
    assert finished["result_json_path"]

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 2
    assert items[0]["top1_book_name"] == "测试小说"
    assert observed_thresholds == [0.35, 0.35]

    listed_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        task_id=task["task_id"],
        item_status="completed",
    )
    assert len(listed_results) == 2
    assert listed_results[0]["review"]["review_status"] == ""

    updated = upsert_compare_task_review(
        business_db,
        listed_results[0]["result_id"],
        review_status="确认高疑似",
        reviewer_name="pytest",
        review_note="命中样本正常",
    )
    assert updated is not None
    assert updated["review"]["review_status"] == "确认高疑似"
    assert updated["review"]["reviewer_name"] == "pytest"

    result = get_compare_result(business_db, updated["result_id"])
    assert result is not None
    assert result["result_payload"]["fine"]["results"][0]["book_name"] == "测试小说"
    assert result["review"]["review_status"] == "确认高疑似"

    summary_payload = json.loads((tmp_path / finished["result_json_path"]).read_text(encoding="utf-8"))
    assert summary_payload["task_id"] == task["task_id"]


def test_enrich_result_payload_for_review_adds_long_candidate_context(tmp_path: Path) -> None:
    retrieval_db = tmp_path / "retrieval_review.sqlite3"
    conn = sqlite3.connect(retrieval_db)
    try:
        conn.executescript(
            """
            CREATE TABLE chapter_contents (
                chapter_uid INTEGER PRIMARY KEY,
                content_raw TEXT,
                content_clean TEXT,
                content_retrieval TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        chapter_text = "开头段落。" + ("这是完整章节正文。" * 300) + "结尾段落。"
        conn.execute(
            """
            INSERT INTO chapter_contents (
                chapter_uid, content_raw, content_clean, content_retrieval, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, '2026-05-16 10:00:00', '2026-05-16 10:00:00')
            """,
            (1, chapter_text, chapter_text, chapter_text),
        )
        conn.commit()
    finally:
        conn.close()

    payload = {
        "fine": {
            "results": [
                {
                    "chapter_uid": 1,
                    "best_match": {
                        "candidate_start_offset": 120,
                        "candidate_end_offset": 220,
                        "matched_substring": "这是完整章节正文",
                        "candidate_text": "短窗口",
                    },
                }
            ]
        }
    }
    enriched = _enrich_result_payload_for_review(retrieval_db, payload)
    best_match = enriched["fine"]["results"][0]["best_match"]
    assert "candidate_review_context_text" in best_match
    assert "candidate_text_full" in best_match
    assert len(best_match["candidate_review_context_text"]) > len(best_match["candidate_text"])
    assert len(best_match["candidate_text_full"]) > len(best_match["candidate_text"])


def test_batch_task_parallelism_smoke(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_parallel.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batch_parallel.txt"
    input_path.write_text("第一条\n\n第二条\n\n第三条\n\n第四条", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-parallel-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256",
        source_file_size=input_path.stat().st_size,
        params={
            "top_k": 3,
            "compare_top_k": 5,
            "merged_top_k": 8,
            "candidate_display_score_threshold": 0.01,
        },
        created_by="pytest",
    )

    active = 0
    max_active = 0
    lock = threading.Lock()

    def slow_compare_pipeline(request):  # type: ignore[no-untyped-def]
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.2)
            return _fake_compare_pipeline(request)
        finally:
            with lock:
                active -= 1

    started_at = time.perf_counter()
    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_parallel.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_parallel",
        item_parallelism=2,
        compare_fn=slow_compare_pipeline,
    )
    elapsed = time.perf_counter() - started_at

    assert finished["status"] == "completed"
    assert finished["counts"]["accepted"] == 4
    assert finished["counts"]["completed"] == 4
    assert max_active == 2
    assert elapsed < 0.75


def test_pause_resume_and_soft_delete_task_smoke(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_pause.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batch_pause.txt"
    input_path.write_text("第一条\n\n第二条\n\n第三条", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-pause-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    paused_before_run = pause_compare_task(
        db_path=business_db,
        task_id=task["task_id"],
        reason="pause before run",
    )
    assert paused_before_run is not None
    assert paused_before_run["status"] == "paused"

    resumed = resume_compare_task(
        db_path=business_db,
        task_id=task["task_id"],
        reason="resume before run",
    )
    assert resumed is not None
    assert resumed["status"] == "queued"
    claimed = claim_next_compare_task(
        db_path=business_db,
        worker_name="pytest-worker",
    )
    assert claimed is not None
    assert claimed["status"] == "running"

    allow_pause = threading.Event()
    started_once = threading.Event()

    def pausable_compare_pipeline(request):  # type: ignore[no-untyped-def]
        started_once.set()
        allow_pause.wait(timeout=5)
        return _fake_compare_pipeline(request)

    result_holder: dict[str, dict[str, object]] = {}

    def run_task() -> None:
        result_holder["task"] = execute_claimed_task(
            task_id=task["task_id"],
            business_db_path=business_db,
            retrieval_db_path=tmp_path / "retrieval_pause.sqlite3",
            semantic_config=SemanticRetrievalConfig(),
            export_root=tmp_path / "exports_pause",
            compare_fn=pausable_compare_pipeline,
        )

    worker = threading.Thread(target=run_task, daemon=True)
    worker.start()
    assert started_once.wait(timeout=2)

    paused_during_run = pause_compare_task(
        db_path=business_db,
        task_id=task["task_id"],
        reason="pause during run",
    )
    assert paused_during_run is not None
    assert paused_during_run["status"] == "pause_requested"

    allow_pause.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    paused_after_run = get_compare_task(business_db, task["task_id"])
    assert paused_after_run is not None
    assert paused_after_run["status"] == "paused"
    assert paused_after_run["counts"]["completed"] == 1

    resumed_after_pause = resume_compare_task(
        db_path=business_db,
        task_id=task["task_id"],
        reason="resume remaining work",
    )
    assert resumed_after_pause is not None
    assert resumed_after_pause["status"] == "queued"

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_pause.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_pause",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"
    assert finished["counts"]["completed"] == 3
    assert finished["counts"]["failed"] == 0

    deleted = delete_compare_task(
        db_path=business_db,
        task_id=task["task_id"],
        reason="cleanup",
    )
    assert deleted is not None
    assert deleted["is_deleted"] is True

    assert get_compare_task(business_db, task["task_id"]) is None
    assert list_compare_tasks(business_db, limit=20, offset=0) == []
    assert list_compare_results(business_db, limit=20, offset=0) == []


def test_recover_interrupted_tasks_recovers_orphaned_worker_states(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_recover.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batch_recover.txt"
    input_path.write_text("第一条\n\n第二条\n\n第三条", encoding="utf-8")

    def create_task(task_id: str) -> dict[str, object]:
        task = create_compare_task(
            db_path=business_db,
            task_id=task_id,
            detection_mode="rewrite",
            source_file_name=input_path.name,
            source_file_ext=".txt",
            source_file_path=str(input_path),
            source_file_sha256=f"sha256-{task_id}",
            source_file_size=input_path.stat().st_size,
            params={"candidate_display_score_threshold": 0.01},
            created_by="pytest",
        )
        replace_task_items(
            db_path=business_db,
            task_id=task_id,
            items=[
                {"item_order": 1, "source_ref": "1", "query_text": "第一条"},
                {"item_order": 2, "source_ref": "2", "query_text": "第二条"},
                {"item_order": 3, "source_ref": "3", "query_text": "第三条"},
            ],
        )
        set_task_input_count(
            db_path=business_db,
            task_id=task_id,
            accepted_input_count=3,
            status_message="seed items",
        )
        return task

    running_task = create_task("task-recover-running")
    cancel_task = create_task("task-recover-cancel")
    pause_task = create_task("task-recover-pause")

    conn = sqlite3.connect(business_db)
    try:
        for task_id, status in (
            (str(running_task["task_id"]), "running"),
            (str(cancel_task["task_id"]), "cancel_requested"),
            (str(pause_task["task_id"]), "pause_requested"),
        ):
            conn.execute(
                """
                UPDATE compare_tasks
                   SET status = ?,
                       worker_name = 'dead-worker',
                       started_at = '2026-05-27 16:00:00',
                       updated_at = '2026-05-27 16:10:00',
                       last_heartbeat_at = '2026-05-27 16:10:00'
                 WHERE task_id = ?
                """,
                (status, task_id),
            )
            conn.execute(
                """
                UPDATE compare_task_items
                   SET status = 'completed',
                       started_at = '2026-05-27T16:00:01',
                       finished_at = '2026-05-27T16:00:03',
                       duration_seconds = 2.0,
                       updated_at = '2026-05-27T16:00:03'
                 WHERE task_id = ?
                   AND item_order = 1
                """,
                (task_id,),
            )
            conn.execute(
                """
                UPDATE compare_task_items
                   SET status = 'running',
                       started_at = '2026-05-27T16:00:04',
                       finished_at = NULL,
                       duration_seconds = NULL,
                       updated_at = '2026-05-27T16:00:04'
                 WHERE task_id = ?
                   AND item_order = 2
                """,
                (task_id,),
            )
        conn.commit()
    finally:
        conn.close()

    summary = recover_interrupted_tasks(business_db)

    assert summary == {
        "running_to_queued": 1,
        "cancel_requested_to_cancelled": 1,
        "pause_requested_to_paused": 1,
        "requeued_item_count": 3,
    }

    recovered_running = get_compare_task(business_db, str(running_task["task_id"]))
    assert recovered_running is not None
    assert recovered_running["status"] == "queued"
    assert recovered_running["worker_name"] == ""
    assert recovered_running["counts"]["completed"] == 0
    running_items = list_compare_task_items(business_db, str(running_task["task_id"]), limit=10, offset=0)
    assert [item["status"] for item in running_items] == ["completed", "queued", "queued"]

    recovered_cancel = get_compare_task(business_db, str(cancel_task["task_id"]), include_deleted=True)
    assert recovered_cancel is not None
    assert recovered_cancel["status"] == "cancelled"
    assert recovered_cancel["worker_name"] == ""
    assert recovered_cancel["finished_at"] is not None
    assert recovered_cancel["counts"]["completed"] == 0
    cancel_items = list_compare_task_items(business_db, str(cancel_task["task_id"]), limit=10, offset=0)
    assert [item["status"] for item in cancel_items] == ["completed", "queued", "queued"]

    recovered_pause = get_compare_task(business_db, str(pause_task["task_id"]))
    assert recovered_pause is not None
    assert recovered_pause["status"] == "paused"
    assert recovered_pause["worker_name"] == ""
    assert recovered_pause["paused_at"] is not None
    assert recovered_pause["counts"]["completed"] == 0
    pause_items = list_compare_task_items(business_db, str(pause_task["task_id"]), limit=10, offset=0)
    assert [item["status"] for item in pause_items] == ["completed", "queued", "queued"]


def test_list_compare_results_supports_backend_filter_pagination_and_stats(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_review.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "review_batch.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条测试文本\n"
        "短剧乙,2,作者乙,第二条测试文本\n"
        "短剧丙,3,作者丙,第三条测试文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-review-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256",
        source_file_size=input_path.stat().st_size,
        owner_user_id=1,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_review.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_review",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    listed_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        task_id=task["task_id"],
        item_status="completed",
    )
    assert len(listed_results) == 3

    first_id = int(listed_results[0]["result_id"])
    second_id = int(listed_results[1]["result_id"])

    updated_high_risk = upsert_compare_task_review(
        business_db,
        first_id,
        review_status="confirmed_high_risk",
        reviewer_name="pytest",
        review_note="hit",
    )
    assert updated_high_risk is not None

    updated_false_positive = upsert_compare_task_review(
        business_db,
        second_id,
        review_status="false_positive",
        reviewer_name="pytest",
        review_note="false alarm",
    )
    assert updated_false_positive is not None

    cleared_count = clear_pending_review_results(
        business_db,
        owner_user_id=int(task["owner_user_id"]),
    )
    assert cleared_count == 1

    filtered_results = list_compare_results(
        business_db,
        limit=1,
        offset=0,
        task_id=task["task_id"],
        item_status="completed",
        text_filter="短剧",
        exclude_cleared=True,
    )
    assert len(filtered_results) == 1

    total = count_compare_results(
        business_db,
        task_id=task["task_id"],
        item_status="completed",
        text_filter="短剧",
        exclude_cleared=True,
    )
    assert total == 2

    stats = summarize_compare_results(
        business_db,
        task_id=task["task_id"],
        item_status="completed",
        text_filter="短剧",
        exclude_cleared=True,
    )
    assert stats["total"] == 2
    assert stats["pending"] == 0
    assert stats["confirmed_high_risk"] == 1
    assert stats["needs_followup"] == 0
    assert stats["false_positive"] == 1


def test_list_compare_results_dedupe_latest_keeps_latest_owner_result(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_review_dedupe.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "review_batch_dedupe.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条重复测试文案\n"
        "短剧乙,2,作者乙,第二条重复测试文案\n",
        encoding="utf-8",
    )

    for task_id in ("task-review-dedupe-001", "task-review-dedupe-002"):
        task = create_compare_task(
            db_path=business_db,
            task_id=task_id,
            detection_mode="rewrite",
            source_file_name=input_path.name,
            source_file_ext=".csv",
            source_file_path=str(input_path),
            source_file_sha256=task_id,
            source_file_size=input_path.stat().st_size,
            owner_user_id=1,
            params={"candidate_display_score_threshold": 0.01},
            created_by="pytest",
        )
        finished = execute_claimed_task(
            task_id=task["task_id"],
            business_db_path=business_db,
            retrieval_db_path=tmp_path / "retrieval_review_dedupe.sqlite3",
            semantic_config=SemanticRetrievalConfig(),
            export_root=tmp_path / "exports_review_dedupe",
            compare_fn=_fake_compare_pipeline,
        )
        assert finished["status"] == "completed"

    all_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        owner_user_id=1,
        dedupe_latest=False,
    )
    deduped_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        owner_user_id=1,
        dedupe_latest=True,
    )

    assert len(all_results) == 4
    assert len(deduped_results) == 2
    assert count_compare_results(
        business_db,
        owner_user_id=1,
        dedupe_latest=True,
    ) == 2

    stats = summarize_compare_results(
        business_db,
        owner_user_id=1,
        dedupe_latest=True,
    )
    assert stats["total"] == 2
    assert stats["pending"] == 2


def test_list_compare_results_owner_fast_path_supports_score_desc(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_review_score.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "review_batch_score.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条排序测试文本\n"
        "短剧乙,2,作者乙,第二条排序测试文本\n",
        encoding="utf-8",
    )

    for task_id in ("task-review-score-001", "task-review-score-002"):
        task = create_compare_task(
            db_path=business_db,
            task_id=task_id,
            detection_mode="rewrite",
            source_file_name=input_path.name,
            source_file_ext=".csv",
            source_file_path=str(input_path),
            source_file_sha256=task_id,
            source_file_size=input_path.stat().st_size,
            owner_user_id=1,
            params={"candidate_display_score_threshold": 0.01},
            created_by="pytest",
        )
        finished = execute_claimed_task(
            task_id=task["task_id"],
            business_db_path=business_db,
            retrieval_db_path=tmp_path / "retrieval_review_score.sqlite3",
            semantic_config=SemanticRetrievalConfig(),
            export_root=tmp_path / "exports_review_score",
            compare_fn=_scored_compare_pipeline,
        )
        assert finished["status"] == "completed"

    scored_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        owner_user_id=1,
        dedupe_latest=False,
        sort_by="score_desc",
    )
    assert [round(float(item["top1_fine_score"]), 2) for item in scored_results[:2]] == [0.83, 0.83]

    deduped_scored_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        owner_user_id=1,
        dedupe_latest=True,
        sort_by="score_desc",
    )
    assert len(deduped_scored_results) == 2
    assert [round(float(item["top1_fine_score"]), 2) for item in deduped_scored_results] == [0.83, 0.41]
    assert {item["task_id"] for item in deduped_scored_results} == {"task-review-score-002"}
