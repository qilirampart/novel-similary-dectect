from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.business_store import (
    backfill_compare_task_item_payload_store_batch,
    backfill_compare_task_item_payload_store,
    cancel_compare_task,
    claim_next_compare_task,
    clear_pending_review_results,
    count_compare_results,
    delete_compare_task,
    _enrich_result_payload_for_review,
    _enrich_result_payload_for_review_with_chapter_texts,
    create_compare_task,
    get_compare_result,
    get_compare_task,
    hydrate_compare_result_summaries,
    init_business_db,
    list_compare_task_input_items,
    list_compare_task_result_payloads,
    list_compare_results,
    list_compare_task_items,
    list_compare_tasks,
    pause_compare_task,
    recover_interrupted_tasks,
    replace_task_items,
    resume_compare_task,
    save_task_item_success,
    set_task_input_count,
    summarize_compare_results,
    upsert_compare_task_review,
)
from service.review_export import build_review_export_xlsx_with_metrics
from service.semantic_retrieval import SemanticRetrievalConfig
from service.task_executor import execute_claimed_task, run_next_queued_task


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


def _prime_legacy_hot_payload(
    business_db: Path,
    result_ids: list[int] | tuple[int, ...],
) -> None:
    normalized_result_ids = [int(result_id) for result_id in result_ids if int(result_id or 0) > 0]
    if not normalized_result_ids:
        return
    conn = sqlite3.connect(business_db)
    try:
        conn.executemany(
            """
            UPDATE compare_task_items
               SET result_payload_json = (
                   SELECT p.result_payload_json
                     FROM compare_task_item_payloads p
                    WHERE p.result_id = compare_task_items.result_id
               )
             WHERE result_id = ?
            """,
            [(result_id,) for result_id in normalized_result_ids],
        )
        conn.commit()
    finally:
        conn.close()


def test_compare_pipeline_marks_semantic_timeout_as_distinct_fallback(monkeypatch) -> None:
    from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline
    from service.semantic_retrieval import SemanticRetrievalConfig, SemanticRetrievalError

    def fake_lexical(**kwargs):  # type: ignore[no-untyped-def]
        return {
            "target_count": 1,
            "candidate_limit_per_index": 10,
            "merged_top_k": 5,
            "index_results": [],
            "results": [
                {
                    "chapter_uid": 1,
                    "dataset_key": "self_short_novels",
                    "book_ext_id": "book-1",
                    "book_name": "测试小说",
                    "chapter_ext_id": "1",
                    "chapter_name": "第1章",
                    "final_score": 0.42,
                    "coarse_score": 0.42,
                    "ngram_score": 0.42,
                    "seed_hit_count": 1,
                    "seed_hit_weight": 1.0,
                    "source_dataset_key": "self_short_novels",
                    "source_table_name": "test_table",
                    "recall_source": "lexical",
                }
            ],
        }

    def fake_semantic(**kwargs):  # type: ignore[no-untyped-def]
        raise SemanticRetrievalError("Request timed out for https://example.com/embeddings: timed out")

    def fake_slice(**kwargs):  # type: ignore[no-untyped-def]
        return [
            {
                "chapter_uid": 1,
                "windows": [
                    {
                        "candidate_window_order": 1,
                        "candidate_start_offset": 0,
                        "candidate_end_offset": 50,
                        "candidate_text": "候选文本",
                    }
                ],
            }
        ]

    def fake_compare(**kwargs):  # type: ignore[no-untyped-def]
        return [
            {
                "fine_rank": 1,
                "review_label": "高风险",
                "confidence_label": "strong",
                "fine_score": 0.88,
                "coarse_rank": 1,
                "coarse_final_score": 0.42,
                "dataset_key": "self_short_novels",
                "book_ext_id": "book-1",
                "book_name": "测试小说",
                "chapter_uid": 1,
                "chapter_ext_id": "1",
                "chapter_name": "第1章",
                "best_match": {
                    "candidate_window_order": 1,
                    "candidate_start_offset": 0,
                    "candidate_end_offset": 50,
                    "exact_substring_hit": False,
                    "longest_match_len": 10,
                    "longest_match_ratio": 0.4,
                    "ngram_recall": 0.5,
                    "ngram_precision": 0.5,
                    "jaccard": 0.5,
                    "sequence_ratio": 0.5,
                    "matched_substring": "测试片段",
                    "query_text_preview": "测试片段",
                    "candidate_text_preview": "候选文本",
                    "candidate_text": "候选文本",
                },
            }
        ]

    monkeypatch.setattr("service.compare_pipeline.global_retrieve_candidates", fake_lexical)
    monkeypatch.setattr("service.compare_pipeline.semantic_retrieve_candidates", fake_semantic)
    monkeypatch.setattr("service.compare_pipeline.slice_candidate_chapters", fake_slice)
    monkeypatch.setattr("service.compare_pipeline.compare_query_to_candidates", fake_compare)

    payload = run_compare_pipeline(
        ComparePipelineRequest(
            db_path="test.sqlite3",
            detection_mode="rewrite",
            query_text="测试片段",
            semantic_config=SemanticRetrievalConfig(),
        )
    )

    assert payload["rewrite_detection"]["status"] == "fallback_semantic_timeout"
    assert "timed out" in str(payload["coarse"]["semantic"]["error"]).lower()


def test_execute_claimed_task_dual_writes_payload_table(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_batch.txt"
    input_path.write_text("这是一条需要落 payload 的测试文本", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-payload",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_payload.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_payload",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        item_row = conn.execute(
            """
            SELECT result_id,
                   query_text,
                   result_payload_json
              FROM compare_task_items
             WHERE task_id = ?
            """,
            (task["task_id"],),
        ).fetchone()
        assert item_row is not None

        payload_row = conn.execute(
            """
            SELECT result_id,
                   query_text,
                   result_payload_json
              FROM compare_task_item_payloads
             WHERE result_id = ?
            """,
            (int(item_row["result_id"]),),
        ).fetchone()
        assert payload_row is not None
        assert str(item_row["result_payload_json"] or "") == ""
        assert str(payload_row["query_text"]) == str(item_row["query_text"])
        assert json.loads(str(payload_row["result_payload_json"]))["fine"]["results"][0]["book_name"] == "测试小说"
    finally:
        conn.close()


def test_execute_claimed_task_does_not_issue_separate_submit_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import service.task_executor as task_executor

    observed_thresholds.clear()
    business_db = tmp_path / "web_business_no_submit_heartbeat.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "no_submit_heartbeat.txt"
    input_path.write_text("提交阶段不应再单独写 heartbeat", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-no-submit-heartbeat-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-no-submit-heartbeat",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    def _unexpected_mark_task_heartbeat(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("submit path should not call mark_task_heartbeat anymore")

    monkeypatch.setattr(
        task_executor,
        "mark_task_heartbeat",
        _unexpected_mark_task_heartbeat,
        raising=False,
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_no_submit_heartbeat.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_no_submit_heartbeat",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    refreshed = get_compare_task(business_db, task["task_id"])
    assert refreshed is not None
    assert "Processed 1 items" in str(refreshed.get("status_message") or "")


def test_execute_claimed_task_batches_progress_counter_updates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import service.task_executor as task_executor

    observed_thresholds.clear()
    business_db = tmp_path / "web_business_batched_progress.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batched_progress.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条批量计数文本\n"
        "短剧乙,2,作者乙,第二条批量计数文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-batched-progress-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-batched-progress",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    barrier = threading.Barrier(2)

    def _batched_compare(request):  # type: ignore[no-untyped-def]
        barrier.wait(timeout=2)
        return json.loads(json.dumps(_fake_compare_pipeline(request), ensure_ascii=False))

    real_update_task_progress = task_executor.update_task_progress
    real_wait = task_executor.wait
    observed_progress_calls: list[dict[str, object]] = []

    def _observed_update_task_progress(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed_progress_calls.append(dict(kwargs))
        return real_update_task_progress(*args, **kwargs)

    monkeypatch.setattr(
        task_executor,
        "update_task_progress",
        _observed_update_task_progress,
    )
    monkeypatch.setattr(
        task_executor,
        "wait",
        lambda futures, return_when=None: real_wait(futures),  # type: ignore[no-untyped-def]
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_batched_progress.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_batched_progress",
        item_parallelism=2,
        compare_fn=_batched_compare,
    )
    assert finished["status"] == "completed"

    refreshed = get_compare_task(business_db, task["task_id"])
    assert refreshed is not None
    assert int((refreshed.get("counts") or {}).get("completed", 0)) == 2
    assert any(
        int(call.get("completed_delta") or 0) == 2
        and int(call.get("failed_delta") or 0) == 0
        for call in observed_progress_calls
    )


def test_execute_claimed_task_throttles_submit_progress_persistence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import service.task_executor as task_executor

    observed_thresholds.clear()
    business_db = tmp_path / "web_business_submit_progress_throttle.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "submit_progress_throttle.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条提交节流文本\n"
        "短剧乙,2,作者乙,第二条提交节流文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-submit-progress-throttle-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-submit-progress-throttle",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    real_mark_task_items_running = task_executor.mark_task_items_running
    observed_touch_flags: list[bool] = []

    def _observed_mark_task_items_running(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed_touch_flags.append(bool(kwargs.get("touch_task_progress", True)))
        return real_mark_task_items_running(*args, **kwargs)

    monkeypatch.setattr(
        task_executor,
        "mark_task_items_running",
        _observed_mark_task_items_running,
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_submit_progress_throttle.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_submit_progress_throttle",
        item_parallelism=2,
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"
    assert observed_touch_flags == [True]


def test_execute_claimed_task_throttles_result_progress_flushes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import service.task_executor as task_executor

    observed_thresholds.clear()
    business_db = tmp_path / "web_business_result_progress_throttle.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "result_progress_throttle.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条结果节流文本\n"
        "短剧乙,2,作者乙,第二条结果节流文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-result-progress-throttle-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-result-progress-throttle",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    real_update_task_progress = task_executor.update_task_progress
    observed_progress_calls: list[dict[str, object]] = []

    def _observed_update_task_progress(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed_progress_calls.append(dict(kwargs))
        return real_update_task_progress(*args, **kwargs)

    def _staggered_compare(request):  # type: ignore[no-untyped-def]
        text = str(request.query_text)
        time.sleep(0.05 if "第一条" in text else 0.10)
        return json.loads(json.dumps(_fake_compare_pipeline(request), ensure_ascii=False))

    monkeypatch.setattr(
        task_executor,
        "update_task_progress",
        _observed_update_task_progress,
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_result_progress_throttle.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_result_progress_throttle",
        item_parallelism=2,
        compare_fn=_staggered_compare,
    )
    assert finished["status"] == "completed"

    counted_flushes = [
        call
        for call in observed_progress_calls
        if int(call.get("completed_delta") or 0) > 0 or int(call.get("failed_delta") or 0) > 0
    ]
    assert len(counted_flushes) == 1
    assert int(counted_flushes[0].get("completed_delta") or 0) == 2
    assert int(counted_flushes[0].get("failed_delta") or 0) == 0


def test_execute_claimed_task_persists_live_completed_counts_before_refill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import service.task_executor as task_executor

    observed_thresholds.clear()
    business_db = tmp_path / "web_business_live_progress_refill.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "live_progress_refill.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条需要先完成的文本\n"
        "短剧乙,2,作者乙,第二条需要阻塞观察的文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-live-progress-refill-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-live-progress-refill",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    second_item_started = threading.Event()
    allow_second_item_finish = threading.Event()

    def _long_then_blocked_compare(request):  # type: ignore[no-untyped-def]
        text = str(request.query_text)
        if "第一条" in text:
            time.sleep(2.2)
            return json.loads(json.dumps(_fake_compare_pipeline(request), ensure_ascii=False))
        second_item_started.set()
        allow_second_item_finish.wait(timeout=5)
        return json.loads(json.dumps(_fake_compare_pipeline(request), ensure_ascii=False))

    observed_progress_calls: list[dict[str, object]] = []
    real_update_task_progress = task_executor.update_task_progress

    def _observed_update_task_progress(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed_progress_calls.append(dict(kwargs))
        return real_update_task_progress(*args, **kwargs)

    monkeypatch.setattr(task_executor, "update_task_progress", _observed_update_task_progress)

    result_holder: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "task",
            execute_claimed_task(
                task_id=task["task_id"],
                business_db_path=business_db,
                retrieval_db_path=tmp_path / "retrieval_live_progress_refill.sqlite3",
                semantic_config=SemanticRetrievalConfig(),
                export_root=tmp_path / "exports_live_progress_refill",
                item_parallelism=1,
                compare_fn=_long_then_blocked_compare,
            ),
        ),
        daemon=True,
    )
    worker.start()

    assert second_item_started.wait(timeout=6)

    mid_task = get_compare_task(business_db, task["task_id"])
    assert mid_task is not None
    assert int((mid_task.get("counts") or {}).get("accepted", 0)) == 2
    assert int((mid_task.get("counts") or {}).get("completed", 0)) == 1
    assert any(int(call.get("completed_delta") or 0) == 1 for call in observed_progress_calls)

    allow_second_item_finish.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    finished = result_holder.get("task")
    assert isinstance(finished, dict)
    assert finished["status"] == "completed"


def test_execute_claimed_task_resume_syncs_stale_counts_and_status_message(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_resume_progress_baseline.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "resume_progress_baseline.txt"
    input_path.write_text("ROW-1\nROW-2\nROW-3", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-resume-progress-baseline-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-resume-progress-baseline",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    replace_task_items(
        db_path=business_db,
        task_id=task["task_id"],
        items=[
            {"item_order": 1, "source_ref": "1", "query_text": "ROW-1"},
            {"item_order": 2, "source_ref": "2", "query_text": "ROW-2"},
            {"item_order": 3, "source_ref": "3", "query_text": "ROW-3"},
        ],
    )
    set_task_input_count(
        db_path=business_db,
        task_id=task["task_id"],
        accepted_input_count=3,
        status_message="seeded inputs",
    )

    seeded_payload = _fake_compare_pipeline(
        SimpleNamespace(
            query_text="ROW-1",
            detection_mode="rewrite",
            candidate_display_score_threshold=0.01,
        )
    )
    seeded_top1 = seeded_payload["fine"]["results"][0]
    save_task_item_success(
        db_path=business_db,
        task_id=task["task_id"],
        item_order=1,
        semantic_status=str(seeded_payload["rewrite_detection"]["status"]),
        top1_book_name=str(seeded_top1["book_name"]),
        top1_chapter_name=str(seeded_top1["chapter_name"]),
        top1_review_label=str(seeded_top1["review_label"]),
        top1_confidence_label=str(seeded_top1["confidence_label"]),
        top1_fine_score=float(seeded_top1["fine_score"]),
        result_payload=seeded_payload,
        update_task_counts=False,
    )

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            """
            UPDATE compare_tasks
               SET completed_input_count = 0,
                   failed_input_count = 0,
                   status_message = 'stale progress before resume'
             WHERE task_id = ?
            """,
            (task["task_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    second_item_started = threading.Event()
    allow_second_item_finish = threading.Event()

    def _resume_compare(request):  # type: ignore[no-untyped-def]
        if str(request.query_text) == "ROW-2":
            second_item_started.set()
            allow_second_item_finish.wait(timeout=5)
        return json.loads(json.dumps(_fake_compare_pipeline(request), ensure_ascii=False))

    result_holder: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "task",
            execute_claimed_task(
                task_id=task["task_id"],
                business_db_path=business_db,
                retrieval_db_path=tmp_path / "retrieval_resume_progress_baseline.sqlite3",
                semantic_config=SemanticRetrievalConfig(),
                export_root=tmp_path / "exports_resume_progress_baseline",
                item_parallelism=1,
                compare_fn=_resume_compare,
            ),
        ),
        daemon=True,
    )
    worker.start()

    assert second_item_started.wait(timeout=5)

    mid_task = get_compare_task(business_db, task["task_id"])
    assert mid_task is not None
    assert int((mid_task.get("counts") or {}).get("accepted", 0)) == 3
    assert int((mid_task.get("counts") or {}).get("completed", 0)) == 1
    assert int((mid_task.get("counts") or {}).get("failed", 0)) == 0
    assert str(mid_task.get("status_message") or "").startswith("Processing item 2/3")

    allow_second_item_finish.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    finished = result_holder.get("task")
    assert isinstance(finished, dict)
    assert finished["status"] == "completed"
    assert finished["counts"]["completed"] == 3


def test_get_compare_result_prefers_payload_table_when_legacy_payload_columns_are_blank(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload_detail.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_detail_batch.txt"
    query_text = "这是一条详情优先从 payload 表读取的测试文本"
    input_path.write_text(query_text, encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-detail-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-payload-detail",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_payload_detail.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_payload_detail",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 1
    result_id = int(items[0]["result_id"])
    _prime_legacy_hot_payload(business_db, [result_id])

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET query_text = '',
                   result_payload_json = ''
             WHERE result_id = ?
            """,
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    detail = get_compare_result(business_db, result_id)
    assert detail is not None
    assert detail["query_text"] == query_text
    assert detail["result_payload"]["fine"]["results"][0]["book_name"] == "测试小说"


def test_get_compare_result_falls_back_to_legacy_payload_when_payload_row_is_missing(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload_detail_legacy_fallback.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_detail_legacy_fallback.txt"
    query_text = "这是一条详情从 legacy payload 回退读取的测试文本"
    input_path.write_text(query_text, encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-detail-legacy-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-payload-detail-legacy",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_payload_detail_legacy.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_payload_detail_legacy",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 1
    result_id = int(items[0]["result_id"])
    _prime_legacy_hot_payload(business_db, [result_id])

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            "DELETE FROM compare_task_item_payloads WHERE result_id = ?",
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    detail = get_compare_result(business_db, result_id)
    assert detail is not None
    assert detail["query_text"] == query_text
    assert detail["result_payload"]["fine"]["results"][0]["book_name"] == "测试小说"


def test_get_compare_result_uses_targeted_legacy_fallback_when_payload_row_is_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload_detail_partial_fallback.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_detail_partial_fallback.txt"
    query_text = "partial payload fallback detail text"
    input_path.write_text(query_text, encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-detail-partial-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-payload-detail-partial",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_payload_detail_partial.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_payload_detail_partial",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 1
    result_id = int(items[0]["result_id"])
    _prime_legacy_hot_payload(business_db, [result_id])

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            """
            UPDATE compare_task_item_payloads
               SET result_payload_json = ''
             WHERE result_id = ?
            """,
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    import service.business_store as business_store

    original_connect_business_db = business_store.connect_business_db
    observed_sql: list[str] = []

    class RecordingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            observed_sql.append(" ".join(str(sql).split()))
            return self._inner.execute(sql, params)

        def close(self):
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def recording_connect(db_path):
        return RecordingConnection(original_connect_business_db(db_path))

    monkeypatch.setattr(
        business_store,
        "connect_business_db",
        recording_connect,
    )

    detail = get_compare_result(business_db, result_id)
    assert detail is not None
    assert detail["query_text"] == query_text
    assert detail["result_payload"]["fine"]["results"][0]["book_name"] == "测试小说"
    targeted_fallback_queries = [
        sql
        for sql in observed_sql
        if "FROM compare_task_items" in sql
        and "WHERE result_id = ?" in sql
        and "payload_result_payload_json" not in sql
    ]
    assert any("SELECT result_payload_json" in sql for sql in targeted_fallback_queries)
    assert not any("SELECT query_text," in sql for sql in targeted_fallback_queries)


def test_list_compare_task_result_payloads_falls_back_to_legacy_payload_when_payload_row_is_missing(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_task_payload_list_legacy.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "task_payload_list_legacy.txt"
    input_path.write_text("第一条\n\n第二条", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-list-legacy-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-task-payload-list-legacy",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_task_payload_list_legacy.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_task_payload_list_legacy",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 2
    first_result_id = int(items[0]["result_id"])
    _prime_legacy_hot_payload(business_db, [first_result_id])

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            "DELETE FROM compare_task_item_payloads WHERE result_id = ?",
            (first_result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    payloads = list_compare_task_result_payloads(business_db, task["task_id"])
    assert len(payloads) == 2
    assert payloads[0]["result_payload"]["fine"]["results"][0]["book_name"] == "测试小说"
    assert payloads[0]["result_payload"]["fine"]["review_rows"][0]["fine_rank"] == 1


def test_hydrate_compare_result_summaries_falls_back_to_legacy_payload_when_payload_row_is_missing(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload_hydrate_legacy.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_hydrate_legacy.txt"
    query_text = "hydrate helper legacy fallback text"
    input_path.write_text(query_text, encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-hydrate-legacy-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-task-payload-hydrate-legacy",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_task_payload_hydrate_legacy.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_task_payload_hydrate_legacy",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    summaries = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        task_id=task["task_id"],
        item_status="completed",
    )
    assert len(summaries) == 1
    result_id = int(summaries[0]["result_id"])
    _prime_legacy_hot_payload(business_db, [result_id])

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            "DELETE FROM compare_task_item_payloads WHERE result_id = ?",
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    hydrated = hydrate_compare_result_summaries(
        business_db,
        summaries,
    )
    assert len(hydrated) == 1
    assert hydrated[0]["query_text"] == query_text
    assert hydrated[0]["result_payload"]["fine"]["results"][0]["book_name"] == "测试小说"


def test_list_compare_task_input_items_prefers_payload_table_when_hot_query_text_is_blank(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload_input.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_input_batch.txt"
    query_text = "payload fallback input text"
    input_path.write_text(query_text, encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-payload-input-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-payload-input",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_payload_input.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_payload_input",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 1
    result_id = int(items[0]["result_id"])

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET query_text = ''
             WHERE result_id = ?
            """,
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    input_items = list_compare_task_input_items(business_db, task["task_id"])
    assert len(input_items) == 1
    assert input_items[0]["query_text"] == query_text


def test_manual_payload_backfill_repairs_missing_payload_rows(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_legacy_payload.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "legacy_backfill_batch.txt"
    query_text = "历史遗留的完整查询文本"
    input_path.write_text(query_text, encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="legacy-task-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-legacy",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_legacy_payload.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_legacy_payload",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 1
    result_id = int(items[0]["result_id"])
    _prime_legacy_hot_payload(business_db, [result_id])

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "DELETE FROM compare_task_item_payloads WHERE result_id = ?",
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    inserted_count = backfill_compare_task_item_payload_store(business_db)
    assert inserted_count == 1

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        payload_row = conn.execute(
            """
            SELECT query_text,
                   result_payload_json
              FROM compare_task_item_payloads
             WHERE result_id = ?
            """,
            (result_id,),
        ).fetchone()
        assert payload_row is not None
        assert str(payload_row["query_text"]) == query_text
        assert json.loads(str(payload_row["result_payload_json"]))["fine"]["results"][0]["book_name"] == "测试小说"
    finally:
        conn.close()


def test_manual_payload_backfill_batch_advances_cursor(tmp_path: Path) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_payload_batch_cursor.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "payload_backfill_cursor_batch.txt"
    input_path.write_text("batch payload cursor seed", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="payload-cursor-task-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-payload-cursor",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )
    replace_task_items(
        business_db,
        task["task_id"],
        [
            {"item_order": 1, "query_text": "第一条待回填文本"},
            {"item_order": 2, "query_text": "第二条待回填文本"},
            {"item_order": 3, "query_text": "第三条待回填文本"},
        ],
    )
    set_task_input_count(business_db, task["task_id"], 3)

    for item_order, query_text in (
        (1, "第一条待回填文本"),
        (2, "第二条待回填文本"),
        (3, "第三条待回填文本"),
    ):
        request = type(
            "PayloadRequest",
            (),
            {
                "query_text": query_text,
                "detection_mode": "rewrite",
                "candidate_display_score_threshold": 0.01,
            },
        )()
        payload = json.loads(
            json.dumps(_fake_compare_pipeline(request), ensure_ascii=False)
        )
        save_task_item_success(
            business_db,
            task["task_id"],
            item_order,
            semantic_status="fallback_lexical_only",
            top1_book_name="测试小说",
            top1_chapter_name="第1章",
            top1_review_label="强证据",
            top1_confidence_label="strong",
            top1_fine_score=0.91,
            result_payload=payload,
        )

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        item_rows = conn.execute(
            """
            SELECT result_id,
                   item_order
              FROM compare_task_items
             WHERE task_id = ?
             ORDER BY result_id ASC
            """,
            (task["task_id"],),
        ).fetchall()
        deleted_result_ids = [int(item_rows[0]["result_id"]), int(item_rows[1]["result_id"])]
        _prime_legacy_hot_payload(business_db, deleted_result_ids)
        conn.executemany(
            "DELETE FROM compare_task_item_payloads WHERE result_id = ?",
            [(result_id,) for result_id in deleted_result_ids],
        )
        conn.commit()
    finally:
        conn.close()

    first_batch = backfill_compare_task_item_payload_store_batch(
        business_db,
        batch_size=1,
        mode="missing",
    )
    assert first_batch["processed_rows"] == 1
    assert first_batch["candidate_rows_before"] == 2
    assert first_batch["candidate_rows_after"] == 1
    assert first_batch["next_result_id"] == deleted_result_ids[0]
    assert first_batch["completed"] is False

    second_batch = backfill_compare_task_item_payload_store_batch(
        business_db,
        batch_size=1,
        last_result_id=first_batch["next_result_id"],
        mode="missing",
    )
    assert second_batch["processed_rows"] == 1
    assert second_batch["candidate_rows_before"] == 1
    assert second_batch["candidate_rows_after"] == 0
    assert second_batch["next_result_id"] == deleted_result_ids[1]
    assert second_batch["completed"] is True

    final_batch = backfill_compare_task_item_payload_store_batch(
        business_db,
        batch_size=1,
        last_result_id=second_batch["next_result_id"],
        mode="missing",
    )
    assert final_batch["processed_rows"] == 0
    assert final_batch["remaining_rows"] == 0
    assert final_batch["completed"] is True

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        restored_rows = conn.execute(
            """
            SELECT result_id,
                   query_text,
                   result_payload_json
              FROM compare_task_item_payloads
             WHERE result_id IN (?, ?)
             ORDER BY result_id ASC
            """,
            tuple(deleted_result_ids),
        ).fetchall()
        assert len(restored_rows) == 2
        assert str(restored_rows[0]["query_text"]) == "第一条待回填文本"
        assert str(restored_rows[1]["query_text"]) == "第二条待回填文本"
        assert json.loads(str(restored_rows[0]["result_payload_json"]))["fine"]["results"][0]["book_name"] == "测试小说"
        assert json.loads(str(restored_rows[1]["result_payload_json"]))["fine"]["results"][0]["book_name"] == "测试小说"
    finally:
        conn.close()


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


def test_two_workers_can_process_two_tasks_concurrently(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_two_workers.sqlite3"
    init_business_db(business_db)

    for task_index in range(2):
        input_path = tmp_path / f"batch_two_workers_{task_index + 1}.txt"
        input_path.write_text("第一条\n第二条", encoding="utf-8")
        create_compare_task(
            db_path=business_db,
            task_id=f"task-two-workers-{task_index + 1:03d}",
            detection_mode="rewrite",
            source_file_name=input_path.name,
            source_file_ext=".txt",
            source_file_path=str(input_path),
            source_file_sha256=f"sha256-two-workers-{task_index + 1}",
            source_file_size=input_path.stat().st_size,
            params={"candidate_display_score_threshold": 0.01},
            created_by="pytest",
        )

    active = 0
    max_active = 0
    lock = threading.Lock()
    start_barrier = threading.Barrier(4)

    def slow_compare_pipeline(request):  # type: ignore[no-untyped-def]
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            start_barrier.wait(timeout=3)
            time.sleep(0.15)
            return _fake_compare_pipeline(request)
        finally:
            with lock:
                active -= 1

    completed_tasks: list[dict[str, Any]] = []
    worker_errors: list[BaseException] = []

    def run_worker(worker_name: str) -> None:
        try:
            task = run_next_queued_task(
                business_db_path=business_db,
                retrieval_db_path=tmp_path / "retrieval_two_workers.sqlite3",
                semantic_config=SemanticRetrievalConfig(),
                export_root=tmp_path / "exports_two_workers",
                worker_name=worker_name,
                item_parallelism=2,
                compare_fn=slow_compare_pipeline,
            )
            if task is not None:
                completed_tasks.append(task)
        except BaseException as exc:  # pragma: no cover - defensive thread capture
            worker_errors.append(exc)

    started_at = time.perf_counter()
    worker_threads = [
        threading.Thread(target=run_worker, args=(f"pytest-worker-{index + 1}",), daemon=True)
        for index in range(2)
    ]
    for thread in worker_threads:
        thread.start()
    for thread in worker_threads:
        thread.join(timeout=10)
    elapsed = time.perf_counter() - started_at

    assert not worker_errors
    assert all(not thread.is_alive() for thread in worker_threads)
    assert len(completed_tasks) == 2
    assert {str(task["task_id"]) for task in completed_tasks} == {
        "task-two-workers-001",
        "task-two-workers-002",
    }
    assert max_active == 4
    assert elapsed < 0.45


def test_two_workers_pause_resume_target_task_without_blocking_other_task(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_two_workers_pause.sqlite3"
    init_business_db(business_db)

    def seed_task(task_id: str, lines: list[str]) -> dict[str, object]:
        input_path = tmp_path / f"{task_id}.txt"
        input_path.write_text("\n".join(lines), encoding="utf-8")
        return create_compare_task(
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

    paused_task = seed_task(
        "task-two-workers-pause-001",
        ["BLOCK-1", "BLOCK-2", "TAIL-3"],
    )
    completed_task = seed_task(
        "task-two-workers-pause-002",
        ["FAST-1", "FAST-2"],
    )

    allow_blocked = threading.Event()
    blocked_started = threading.Event()
    blocked_count = 0
    lock = threading.Lock()

    def pause_compare_pipeline(request):  # type: ignore[no-untyped-def]
        nonlocal blocked_count
        query_text = str(request.query_text)
        if query_text.startswith("BLOCK-"):
            with lock:
                blocked_count += 1
                if blocked_count >= 2:
                    blocked_started.set()
            allow_blocked.wait(timeout=5)
        return _fake_compare_pipeline(request)

    completed_tasks: list[dict[str, Any]] = []
    worker_errors: list[BaseException] = []

    def run_worker(worker_name: str) -> None:
        try:
            task = run_next_queued_task(
                business_db_path=business_db,
                retrieval_db_path=tmp_path / "retrieval_two_workers_pause.sqlite3",
                semantic_config=SemanticRetrievalConfig(),
                export_root=tmp_path / "exports_two_workers_pause",
                worker_name=worker_name,
                item_parallelism=2,
                compare_fn=pause_compare_pipeline,
            )
            if task is not None:
                completed_tasks.append(task)
        except BaseException as exc:  # pragma: no cover - defensive thread capture
            worker_errors.append(exc)

    worker_threads = [
        threading.Thread(target=run_worker, args=(f"pytest-worker-{index + 1}",), daemon=True)
        for index in range(2)
    ]
    for thread in worker_threads:
        thread.start()

    assert blocked_started.wait(timeout=3)

    pause_requested = pause_compare_task(
        db_path=business_db,
        task_id=str(paused_task["task_id"]),
        reason="pause target task during dual worker run",
    )
    assert pause_requested is not None
    assert pause_requested["status"] == "pause_requested"

    allow_blocked.set()

    for thread in worker_threads:
        thread.join(timeout=10)

    assert not worker_errors
    assert all(not thread.is_alive() for thread in worker_threads)
    assert {str(task["task_id"]) for task in completed_tasks} == {
        str(paused_task["task_id"]),
        str(completed_task["task_id"]),
    }

    paused_after_run = get_compare_task(business_db, str(paused_task["task_id"]))
    assert paused_after_run is not None
    assert paused_after_run["status"] == "paused"
    assert paused_after_run["counts"]["completed"] == 2
    paused_items = list_compare_task_items(business_db, str(paused_task["task_id"]), limit=10, offset=0)
    assert [item["status"] for item in paused_items] == ["completed", "completed", "queued"]

    completed_after_run = get_compare_task(business_db, str(completed_task["task_id"]))
    assert completed_after_run is not None
    assert completed_after_run["status"] == "completed"
    assert completed_after_run["counts"]["completed"] == 2

    resumed = resume_compare_task(
        db_path=business_db,
        task_id=str(paused_task["task_id"]),
        reason="resume paused dual worker task",
    )
    assert resumed is not None
    assert resumed["status"] == "queued"

    finished = execute_claimed_task(
        task_id=str(paused_task["task_id"]),
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_two_workers_pause.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_two_workers_pause_resume",
        item_parallelism=2,
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"
    assert finished["counts"]["completed"] == 3


def test_two_workers_cancel_target_task_without_blocking_other_task(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_two_workers_cancel.sqlite3"
    init_business_db(business_db)

    def seed_task(task_id: str, lines: list[str]) -> dict[str, object]:
        input_path = tmp_path / f"{task_id}.txt"
        input_path.write_text("\n".join(lines), encoding="utf-8")
        return create_compare_task(
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

    cancelled_task = seed_task(
        "task-two-workers-cancel-001",
        ["BLOCK-1", "BLOCK-2", "TAIL-3"],
    )
    completed_task = seed_task(
        "task-two-workers-cancel-002",
        ["FAST-1", "FAST-2"],
    )

    allow_blocked = threading.Event()
    blocked_started = threading.Event()
    blocked_count = 0
    lock = threading.Lock()

    def cancel_compare_pipeline(request):  # type: ignore[no-untyped-def]
        nonlocal blocked_count
        query_text = str(request.query_text)
        if query_text.startswith("BLOCK-"):
            with lock:
                blocked_count += 1
                if blocked_count >= 2:
                    blocked_started.set()
            allow_blocked.wait(timeout=5)
        return _fake_compare_pipeline(request)

    completed_tasks: list[dict[str, Any]] = []
    worker_errors: list[BaseException] = []

    def run_worker(worker_name: str) -> None:
        try:
            task = run_next_queued_task(
                business_db_path=business_db,
                retrieval_db_path=tmp_path / "retrieval_two_workers_cancel.sqlite3",
                semantic_config=SemanticRetrievalConfig(),
                export_root=tmp_path / "exports_two_workers_cancel",
                worker_name=worker_name,
                item_parallelism=2,
                compare_fn=cancel_compare_pipeline,
            )
            if task is not None:
                completed_tasks.append(task)
        except BaseException as exc:  # pragma: no cover - defensive thread capture
            worker_errors.append(exc)

    worker_threads = [
        threading.Thread(target=run_worker, args=(f"pytest-worker-{index + 1}",), daemon=True)
        for index in range(2)
    ]
    for thread in worker_threads:
        thread.start()

    assert blocked_started.wait(timeout=3)

    cancel_requested = cancel_compare_task(
        db_path=business_db,
        task_id=str(cancelled_task["task_id"]),
        reason="cancel target task during dual worker run",
    )
    assert cancel_requested is not None
    assert cancel_requested["status"] == "cancel_requested"

    allow_blocked.set()

    for thread in worker_threads:
        thread.join(timeout=10)

    assert not worker_errors
    assert all(not thread.is_alive() for thread in worker_threads)
    assert {str(task["task_id"]) for task in completed_tasks} == {
        str(cancelled_task["task_id"]),
        str(completed_task["task_id"]),
    }

    cancelled_after_run = get_compare_task(business_db, str(cancelled_task["task_id"]), include_deleted=True)
    assert cancelled_after_run is not None
    assert cancelled_after_run["status"] == "cancelled"
    assert cancelled_after_run["counts"]["completed"] == 2
    cancelled_items = list_compare_task_items(
        business_db,
        str(cancelled_task["task_id"]),
        limit=10,
        offset=0,
    )
    assert [item["status"] for item in cancelled_items] == ["completed", "completed", "queued"]

    completed_after_run = get_compare_task(business_db, str(completed_task["task_id"]))
    assert completed_after_run is not None
    assert completed_after_run["status"] == "completed"
    assert completed_after_run["counts"]["completed"] == 2


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


def test_execute_claimed_task_resumes_from_stored_inputs_when_source_file_is_missing(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_resume_missing_source.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batch_resume_missing.txt"
    input_path.write_text("第一条\n\n第二条", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-resume-missing-source",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-resume-missing-source",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )
    replace_task_items(
        db_path=business_db,
        task_id=task["task_id"],
        items=[
            {"item_order": 1, "source_ref": "1", "query_text": "第一条"},
            {"item_order": 2, "source_ref": "2", "query_text": "第二条"},
        ],
    )
    set_task_input_count(
        db_path=business_db,
        task_id=task["task_id"],
        accepted_input_count=2,
        status_message="seeded items",
    )

    input_path.unlink()

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_resume_missing.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_resume_missing",
        compare_fn=_fake_compare_pipeline,
    )

    assert finished["status"] == "completed"
    assert finished["counts"]["completed"] == 2
    assert finished["counts"]["failed"] == 0
    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert [item["status"] for item in items] == ["completed", "completed"]


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


def test_recover_interrupted_tasks_skips_running_task_with_fresh_heartbeat(tmp_path: Path) -> None:
    business_db = tmp_path / "web_business_recover_fresh.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "batch_recover_fresh.txt"
    input_path.write_text("第一条\n第二条", encoding="utf-8")

    task = create_compare_task(
        db_path=business_db,
        task_id="task-recover-fresh",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="sha256-task-recover-fresh",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )
    replace_task_items(
        db_path=business_db,
        task_id=str(task["task_id"]),
        items=[
            {"item_order": 1, "source_ref": "1", "query_text": "第一条"},
            {"item_order": 2, "source_ref": "2", "query_text": "第二条"},
        ],
    )
    set_task_input_count(
        db_path=business_db,
        task_id=str(task["task_id"]),
        accepted_input_count=2,
        status_message="seed items",
    )

    fresh_heartbeat = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            """
            UPDATE compare_tasks
               SET status = 'running',
                   worker_name = 'live-worker',
                   started_at = '2026-05-27 16:00:00',
                   updated_at = ?,
                   last_heartbeat_at = ?
             WHERE task_id = ?
            """,
            (fresh_heartbeat, fresh_heartbeat, str(task["task_id"])),
        )
        conn.execute(
            """
            UPDATE compare_task_items
               SET status = 'running',
                   started_at = ?,
                   finished_at = NULL,
                   duration_seconds = NULL,
                   updated_at = ?
             WHERE task_id = ?
               AND item_order = 1
            """,
            (fresh_heartbeat, fresh_heartbeat, str(task["task_id"])),
        )
        conn.commit()
    finally:
        conn.close()

    summary = recover_interrupted_tasks(
        business_db,
        stale_after_seconds=120.0,
    )

    assert summary == {
        "running_to_queued": 0,
        "cancel_requested_to_cancelled": 0,
        "pause_requested_to_paused": 0,
        "requeued_item_count": 0,
    }
    recovered = get_compare_task(business_db, str(task["task_id"]))
    assert recovered is not None
    assert recovered["status"] == "running"
    assert recovered["worker_name"] == "live-worker"
    items = list_compare_task_items(business_db, str(task["task_id"]), limit=10, offset=0)
    assert [item["status"] for item in items] == ["running", "queued"]


def test_run_next_queued_task_passes_recovery_stale_threshold(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_claim_next_compare_task(*, db_path, worker_name, stale_after_seconds):  # type: ignore[no-untyped-def]
        observed["db_path"] = db_path
        observed["worker_name"] = worker_name
        observed["stale_after_seconds"] = stale_after_seconds
        return None

    monkeypatch.setattr("service.task_executor.claim_next_compare_task", fake_claim_next_compare_task)

    result = run_next_queued_task(
        business_db_path=tmp_path / "business.sqlite3",
        retrieval_db_path=tmp_path / "retrieval.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports",
        worker_name="pytest-worker",
        task_recovery_stale_seconds=45.0,
    )

    assert result is None
    assert observed == {
        "db_path": tmp_path / "business.sqlite3",
        "worker_name": "pytest-worker",
        "stale_after_seconds": 45.0,
    }


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


def test_list_compare_results_dedupe_latest_uses_dedupe_key_when_hot_query_text_is_blank(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_review_dedupe_non_owner.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "review_batch_dedupe_non_owner.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条去重测试文本\n"
        "短剧乙,2,作者乙,第二条去重测试文本\n",
        encoding="utf-8",
    )

    created_task_ids: list[str] = []
    for task_id in ("task-review-dedupe-no-owner-001", "task-review-dedupe-no-owner-002"):
        task = create_compare_task(
            db_path=business_db,
            task_id=task_id,
            detection_mode="rewrite",
            source_file_name=input_path.name,
            source_file_ext=".csv",
            source_file_path=str(input_path),
            source_file_sha256=task_id,
            source_file_size=input_path.stat().st_size,
            params={"candidate_display_score_threshold": 0.01},
            created_by="pytest",
        )
        finished = execute_claimed_task(
            task_id=task["task_id"],
            business_db_path=business_db,
            retrieval_db_path=tmp_path / "retrieval_review_dedupe_non_owner.sqlite3",
            semantic_config=SemanticRetrievalConfig(),
            export_root=tmp_path / "exports_review_dedupe_non_owner",
            compare_fn=_fake_compare_pipeline,
        )
        assert finished["status"] == "completed"
        created_task_ids.append(str(task["task_id"]))

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET query_text = ''
             WHERE task_id = ?
            """,
            (created_task_ids[-1],),
        )
        conn.commit()
    finally:
        conn.close()

    all_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        dedupe_latest=False,
    )
    deduped_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        dedupe_latest=True,
    )

    assert len(all_results) == 4
    assert len(deduped_results) == 2
    assert {item["task_id"] for item in deduped_results} == {created_task_ids[-1]}
    assert count_compare_results(
        business_db,
        dedupe_latest=True,
    ) == 2

    stats = summarize_compare_results(
        business_db,
        dedupe_latest=True,
    )
    assert stats["total"] == 2
    assert stats["pending"] == 2


def test_init_business_db_backfills_dedupe_key_from_payload_query_text_when_hot_text_is_blank(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_dedupe_key_payload_backfill.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "dedupe_key_payload_backfill.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条 dedupe key 文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-dedupe-key-payload-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-dedupe-key-payload",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )
    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_dedupe_key_payload.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_dedupe_key_payload",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    items = list_compare_task_items(business_db, task["task_id"], limit=10, offset=0)
    assert len(items) == 1
    result_id = int(items[0]["result_id"])

    expected_dedupe_key = sha256(
        "\x1f".join(("短剧甲", "__EMPTY__", "1", "第一条 dedupe key 文本")).encode("utf-8")
    ).hexdigest()

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            UPDATE compare_task_items
               SET query_text = '',
                   dedupe_key = ''
             WHERE result_id = ?
            """,
            (result_id,),
        )
        conn.commit()
    finally:
        conn.close()

    init_business_db(business_db)

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT dedupe_key,
                   query_text
              FROM compare_task_items
             WHERE result_id = ?
            """,
            (result_id,),
        ).fetchone()
        assert row is not None
        assert str(row["query_text"] or "") == ""
        assert str(row["dedupe_key"] or "") == expected_dedupe_key
    finally:
        conn.close()


def test_init_business_db_syncs_payload_query_text_when_source_metadata_backfill_repairs_items(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_source_metadata_payload_sync.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "source_metadata_payload_sync.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,源文件中的正确文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-source-metadata-payload-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-source-metadata-payload",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )
    replace_task_items(
        business_db,
        task["task_id"],
        [
            {
                "item_order": 1,
                "source_ref": "row_2",
                "query_text": "旧错误文本",
            }
        ],
    )

    init_business_db(business_db)

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT i.result_id,
                   i.query_text,
                   p.query_text AS payload_query_text,
                   i.source_short_drama
              FROM compare_task_items i
              LEFT JOIN compare_task_item_payloads p
                ON p.result_id = i.result_id
             WHERE i.task_id = ?
            """,
            (task["task_id"],),
        ).fetchone()
        assert row is not None
        assert str(row["query_text"] or "") == "源文件中的正确文本"
        assert str(row["source_short_drama"] or "") == "短剧甲"
        assert str(row["payload_query_text"] or "") == "源文件中的正确文本"

        conn.execute(
            """
            UPDATE compare_task_items
               SET query_text = ''
             WHERE result_id = ?
            """,
            (int(row["result_id"]),),
        )
        conn.commit()
    finally:
        conn.close()

    input_items = list_compare_task_input_items(business_db, task["task_id"])
    assert len(input_items) == 1
    assert input_items[0]["query_text"] == "源文件中的正确文本"


def test_init_business_db_recomputes_dedupe_key_when_source_metadata_backfill_repairs_text(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_source_metadata_dedupe_repair.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "source_metadata_dedupe_repair.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,源文件中的正确文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-source-metadata-dedupe-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-source-metadata-dedupe",
        source_file_size=input_path.stat().st_size,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )
    replace_task_items(
        business_db,
        task["task_id"],
        [
            {
                "item_order": 1,
                "source_ref": "row_2",
                "query_text": "旧错误文本",
            }
        ],
    )

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.execute(
            """
            SELECT dedupe_key
              FROM compare_task_items
             WHERE task_id = ?
            """,
            (task["task_id"],),
        ).fetchone()
        assert before is not None
        stale_dedupe_key = str(before["dedupe_key"] or "")
        assert stale_dedupe_key
    finally:
        conn.close()

    init_business_db(business_db)

    conn = sqlite3.connect(business_db)
    conn.row_factory = sqlite3.Row
    try:
        after = conn.execute(
            """
            SELECT dedupe_key,
                   source_ref,
                   query_text,
                   source_short_drama
              FROM compare_task_items
             WHERE task_id = ?
            """,
            (task["task_id"],),
        ).fetchone()
        assert after is not None
        assert str(after["query_text"] or "") == "源文件中的正确文本"
        assert str(after["source_short_drama"] or "") == "短剧甲"
        expected_dedupe_key = sha256(
            "\x1f".join(
                (
                    "短剧甲",
                    "__EMPTY__",
                    str(after["source_ref"] or ""),
                    "源文件中的正确文本",
                )
            ).encode("utf-8")
        ).hexdigest()
        assert str(after["dedupe_key"] or "") != stale_dedupe_key
        assert str(after["dedupe_key"] or "") == expected_dedupe_key
    finally:
        conn.close()


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


def test_review_export_uses_bulk_payload_hydration_instead_of_per_result_detail_calls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_review_export_bulk.sqlite3"
    init_business_db(business_db)

    input_path = tmp_path / "review_export_bulk.csv"
    input_path.write_text(
        "short_drama,episode,author,query_text\n"
        "短剧甲,1,作者甲,第一条复核导出文本\n"
        "短剧乙,2,作者乙,第二条复核导出文本\n",
        encoding="utf-8",
    )

    task = create_compare_task(
        db_path=business_db,
        task_id="task-review-export-bulk-001",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".csv",
        source_file_path=str(input_path),
        source_file_sha256="sha256-review-export-bulk",
        source_file_size=input_path.stat().st_size,
        owner_user_id=1,
        params={"candidate_display_score_threshold": 0.01},
        created_by="pytest",
    )

    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_review_export_bulk.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_review_export_bulk",
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"

    listed_results = list_compare_results(
        business_db,
        limit=10,
        offset=0,
        task_id=task["task_id"],
        item_status="completed",
        owner_user_id=1,
    )
    assert len(listed_results) == 2
    _prime_legacy_hot_payload(
        business_db,
        [int(listed_results[0]["result_id"])],
    )

    updated_review = upsert_compare_task_review(
        business_db,
        int(listed_results[0]["result_id"]),
        review_status="confirmed_high_risk",
        reviewer_name="pytest",
        review_note="bulk export regression guard",
    )
    assert updated_review is not None

    conn = sqlite3.connect(business_db)
    try:
        conn.execute(
            "DELETE FROM compare_task_item_payloads WHERE result_id = ?",
            (int(listed_results[0]["result_id"]),),
        )
        conn.commit()
    finally:
        conn.close()

    import service.review_export as review_export

    def _unexpected_get_compare_result(*args, **kwargs):
        raise AssertionError("review export should not call get_compare_result per result")

    monkeypatch.setattr(
        review_export,
        "get_compare_result",
        _unexpected_get_compare_result,
        raising=False,
    )

    export_path, download_name, metrics = build_review_export_xlsx_with_metrics(
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_review_export_bulk.sqlite3",
        export_root=tmp_path / "exports_review_export_bulk_xlsx",
        owner_user_id=1,
        task_id=task["task_id"],
    )

    assert export_path.exists()
    assert download_name.endswith(".xlsx")
    assert metrics["summary_count"] >= 1
    assert metrics["detail_count"] >= 1
    assert metrics["total_elapsed_seconds"] >= 0
    assert metrics["stages"]["load_result_summaries_seconds"] >= 0
    assert metrics["stages"]["hydrate_result_details_seconds"] >= 0
    assert metrics["stages"]["build_workbook_seconds"] >= 0
    assert metrics["stages"]["save_workbook_seconds"] >= 0


def test_review_export_only_requests_top1_long_evidence_hydration(tmp_path: Path, monkeypatch) -> None:
    import service.review_export as review_export

    observed_kwargs: dict[str, object] = {}

    def _fake_hydrate(*args, **kwargs):  # type: ignore[no-untyped-def]
        observed_kwargs.update(kwargs)
        return [
            {
                "result_id": 1,
                "task_id": "task-1",
                "item_order": 1,
                "task_status": "completed",
                "detection_mode": "rewrite",
                "semantic_status": "ok",
                "review": {"review_status": "confirmed_high_risk"},
                "result_payload": {
                    "fine": {
                        "results": [],
                        "review_rows": [],
                    }
                },
            }
        ]

    monkeypatch.setattr(review_export, "hydrate_compare_result_summaries", _fake_hydrate)
    monkeypatch.setattr(
        review_export,
        "_load_result_summaries",
        lambda **kwargs: [  # type: ignore[no-untyped-def]
            {
                "result_id": 1,
                "task_id": "task-1",
            }
        ],
    )

    export_path, _, metrics = build_review_export_xlsx_with_metrics(
        business_db_path=tmp_path / "unused.sqlite3",
        retrieval_db_path=tmp_path / "unused_retrieval.sqlite3",
        export_root=tmp_path / "exports_review_export_top1_only",
        owner_user_id=1,
    )

    assert export_path.exists()
    assert metrics["detail_count"] == 1
    assert observed_kwargs["review_result_limit"] == 1


def test_review_enrichment_can_limit_to_top1_result() -> None:
    payload = {
        "fine": {
            "results": [
                {
                    "chapter_uid": 101,
                    "best_match": {
                        "candidate_start_offset": 0,
                        "candidate_end_offset": 5,
                        "matched_substring": "alpha",
                    },
                },
                {
                    "chapter_uid": 202,
                    "best_match": {
                        "candidate_start_offset": 0,
                        "candidate_end_offset": 4,
                        "matched_substring": "beta",
                    },
                },
            ]
        }
    }

    enriched = _enrich_result_payload_for_review_with_chapter_texts(
        payload,
        {
            101: "alpha chapter text",
            202: "beta chapter text",
        },
        max_results=1,
    )

    first_match = enriched["fine"]["results"][0]["best_match"]
    second_match = enriched["fine"]["results"][1]["best_match"]

    assert first_match["candidate_text_full"]
    assert first_match["candidate_review_context_text"]
    assert "candidate_text_full" not in second_match
    assert "candidate_review_context_text" not in second_match


def test_worker_shutdown_requeues_inflight_compare_task_without_waiting(
    tmp_path: Path,
) -> None:
    observed_thresholds.clear()
    business_db = tmp_path / "web_business_shutdown.sqlite3"
    init_business_db(business_db)
    input_path = tmp_path / "shutdown_input.txt"
    input_path.write_text("第一条\n\n第二条\n\n第三条", encoding="utf-8")
    task = create_compare_task(
        db_path=business_db,
        task_id="task-worker-shutdown",
        detection_mode="rewrite",
        source_file_name=input_path.name,
        source_file_ext=".txt",
        source_file_path=str(input_path),
        source_file_sha256="shutdown-sha",
        source_file_size=input_path.stat().st_size,
        params={},
    )
    claimed = claim_next_compare_task(business_db, worker_name="shutdown-worker")
    assert claimed is not None

    started = threading.Event()
    release = threading.Event()
    stop_event = threading.Event()
    result_holder: dict[str, dict[str, object]] = {}

    def blocking_compare(request):  # type: ignore[no-untyped-def]
        started.set()
        release.wait(timeout=10)
        return _fake_compare_pipeline(request)

    def run_task() -> None:
        result_holder["task"] = execute_claimed_task(
            task_id=task["task_id"],
            business_db_path=business_db,
            retrieval_db_path=tmp_path / "retrieval_shutdown.sqlite3",
            semantic_config=SemanticRetrievalConfig(),
            export_root=tmp_path / "exports_shutdown",
            item_parallelism=1,
            compare_fn=blocking_compare,
            stop_event=stop_event,
        )

    worker = threading.Thread(target=run_task, daemon=True)
    worker.start()
    assert started.wait(timeout=2)
    stop_started_at = time.perf_counter()
    stop_event.set()
    worker.join(timeout=2)
    stop_elapsed = time.perf_counter() - stop_started_at

    assert not worker.is_alive()
    assert stop_elapsed < 1.5
    assert result_holder["task"]["status"] == "queued"
    requeued_items = list_compare_task_items(business_db, task["task_id"], limit=20)
    assert requeued_items
    assert all(item["status"] == "queued" for item in requeued_items)

    release.set()
    reclaimed = claim_next_compare_task(business_db, worker_name="replacement-worker")
    assert reclaimed is not None
    finished = execute_claimed_task(
        task_id=task["task_id"],
        business_db_path=business_db,
        retrieval_db_path=tmp_path / "retrieval_shutdown.sqlite3",
        semantic_config=SemanticRetrievalConfig(),
        export_root=tmp_path / "exports_shutdown",
        item_parallelism=1,
        compare_fn=_fake_compare_pipeline,
    )
    assert finished["status"] == "completed"
    assert finished["counts"]["completed"] == 3
