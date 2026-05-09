from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.business_store import (
    create_compare_task,
    get_compare_result,
    init_business_db,
    list_compare_results,
    list_compare_task_items,
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
