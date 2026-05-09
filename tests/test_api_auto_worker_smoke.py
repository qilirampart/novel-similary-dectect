from __future__ import annotations

from importlib import reload
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_api_auto_worker_smoke(tmp_path: Path, monkeypatch) -> None:
    business_db = tmp_path / "web_business.sqlite3"
    retrieval_db = tmp_path / "retrieval.sqlite3"
    upload_root = tmp_path / "uploads"
    export_root = tmp_path / "exports"
    retrieval_db.touch()

    monkeypatch.setenv("NOVEL_SIMILARITY_AUTOSTART_WORKER", "1")
    monkeypatch.setenv("NOVEL_SIMILARITY_AUTOWORKER_POLL_SECONDS", "0.2")
    monkeypatch.setenv("NOVEL_SIMILARITY_BUSINESS_DB", str(business_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_DB", str(retrieval_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_UPLOAD_ROOT", str(upload_root))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_EXPORT_ROOT", str(export_root))

    import api.config as api_config
    import api.runtime_worker as runtime_worker
    import api.app as api_app

    reload(api_config)
    runtime_worker = reload(runtime_worker)
    api_app = reload(api_app)

    from service.task_executor import ComparePipelineRequest
    from service.task_executor import run_next_queued_task as real_run_next_queued_task

    captured_thresholds: list[float | None] = []

    def fake_pipeline(request: ComparePipelineRequest) -> Dict[str, Any]:
        captured_thresholds.append(request.candidate_display_score_threshold)
        return {
            "query_text": request.query_text,
            "detection_mode": request.detection_mode,
            "rewrite_detection": {
                "status": "fallback_lexical_only",
            },
            "fine": {
                "results": [
                    {
                        "book_name": "Smoke Novel",
                        "chapter_name": "Chapter 1",
                        "review_label": "high_risk",
                        "confidence_label": "strong",
                        "fine_score": 0.91,
                    }
                ],
                "review_rows": [
                    {
                        "detection_mode": request.detection_mode,
                        "fine_rank": 1,
                        "review_label": "high_risk",
                        "confidence_label": "strong",
                        "fine_score": 0.91,
                        "coarse_rank": 1,
                        "coarse_final_score": 0.83,
                        "dataset_key": "smoke_dataset",
                        "book_ext_id": "book-1",
                        "book_name": "Smoke Novel",
                        "chapter_uid": 1,
                        "chapter_ext_id": "chapter-1",
                        "chapter_name": "Chapter 1",
                        "candidate_window_order": 1,
                        "candidate_start_offset": 0,
                        "candidate_end_offset": 128,
                        "exact_substring_hit": True,
                        "longest_match_len": len(request.query_text),
                        "longest_match_ratio": 1.0,
                        "ngram_recall": 1.0,
                        "ngram_precision": 1.0,
                        "jaccard": 1.0,
                        "sequence_ratio": 1.0,
                        "matched_substring": request.query_text,
                        "query_text_preview": request.query_text,
                        "candidate_text_preview": request.query_text,
                        "query_text": request.query_text,
                        "candidate_text": request.query_text,
                    }
                ],
            },
        }

    def run_next_with_fake_pipeline(**kwargs: Any) -> Optional[Dict[str, Any]]:
        return real_run_next_queued_task(compare_fn=fake_pipeline, **kwargs)

    monkeypatch.setattr(runtime_worker, "run_next_queued_task", run_next_with_fake_pipeline)

    with TestClient(api_app.app) as client:
        create_response = client.post(
            "/api/v1/tasks",
            files={"file": ("batch.txt", b"first query\n\nsecond query", "text/plain")},
            data={
                "detection_mode": "rewrite",
                "candidate_display_score_threshold": "0.35",
            },
        )
        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]

        deadline = time.time() + 10
        last_payload: Optional[Dict[str, Any]] = None
        while time.time() < deadline:
            detail_response = client.get(f"/api/v1/tasks/{task_id}")
            assert detail_response.status_code == 200
            last_payload = detail_response.json()
            if last_payload["task"]["status"] == "completed":
                break
            time.sleep(0.2)

        assert last_payload is not None
        assert last_payload["task"]["status"] == "completed"
        assert last_payload["task"]["counts"]["accepted"] == 2
        assert last_payload["task"]["counts"]["completed"] == 2
        assert last_payload["task"]["counts"]["failed"] == 0
        assert last_payload["task"]["params"]["candidate_display_score_threshold"] == 0.35
        assert len(last_payload["items"]) == 2
        assert last_payload["items"][0]["top1_book_name"] == "Smoke Novel"
        assert captured_thresholds == [0.35, 0.35]
        assert Path(last_payload["task"]["summary_export_path"]).name == "task_summary.csv"
        assert Path(last_payload["task"]["review_export_path"]).name == "task_review_rows.csv"
        assert Path(last_payload["task"]["result_json_path"]).name == "task_summary.json"

        summary_response = client.get(f"/api/v1/tasks/{task_id}/exports/summary")
        assert summary_response.status_code == 200
        assert summary_response.headers["content-type"].startswith("text/csv")
        assert 'filename="task_summary.csv"' in summary_response.headers["content-disposition"]

        review_response = client.get(f"/api/v1/tasks/{task_id}/exports/review")
        assert review_response.status_code == 200
        assert review_response.headers["content-type"].startswith("text/csv")
        assert 'filename="task_review_rows.csv"' in review_response.headers["content-disposition"]

        json_response = client.get(f"/api/v1/tasks/{task_id}/exports/json")
        assert json_response.status_code == 200
        assert json_response.headers["content-type"].startswith("application/json")
        assert 'filename="task_summary.json"' in json_response.headers["content-disposition"]
        json_payload = json_response.json()
        assert json_payload["task_id"] == task_id
        assert json_payload["status"] == "completed"
        assert len(json_payload["items"]) == 2

        unsupported_export_response = client.get(f"/api/v1/tasks/{task_id}/exports/unknown")
        assert unsupported_export_response.status_code == 400
        assert unsupported_export_response.json()["detail"] == "unsupported export kind"

        system_status_response = client.get("/api/v1/system/status")
        assert system_status_response.status_code == 200
        system_payload = system_status_response.json()["payload"]
        assert len(system_payload["cards"]) >= 1
        assert len(system_payload["services"]) >= 1
        assert "recent_exceptions" in system_payload
        assert "slow_tasks" in system_payload
        assert any(card["label"] == "Platform Health" and card["value"] == "healthy" for card in system_payload["cards"])
        assert any(card["label"] == "Stored Results" and card["value"] == "2" for card in system_payload["cards"])
        runtime_paths_service = next(service for service in system_payload["services"] if service["title"] == "Runtime Paths")
        runtime_path_items = {label: value for label, value in runtime_paths_service["items"]}
        assert runtime_path_items["export root"] == str(export_root)
        assert runtime_path_items["upload root"] == str(upload_root)
        assert system_payload["recent_exceptions"][0]["type"] == "normal"
        assert any(task["id"] == task_id and task["status"] == "completed" for task in system_payload["slow_tasks"])


def test_api_result_review_smoke(tmp_path: Path, monkeypatch) -> None:
    business_db = tmp_path / "web_business.sqlite3"
    retrieval_db = tmp_path / "retrieval.sqlite3"
    upload_root = tmp_path / "uploads"
    export_root = tmp_path / "exports"

    monkeypatch.setenv("NOVEL_SIMILARITY_AUTOSTART_WORKER", "1")
    monkeypatch.setenv("NOVEL_SIMILARITY_AUTOWORKER_POLL_SECONDS", "0.2")
    monkeypatch.setenv("NOVEL_SIMILARITY_BUSINESS_DB", str(business_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_DB", str(retrieval_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_UPLOAD_ROOT", str(upload_root))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_EXPORT_ROOT", str(export_root))

    import api.config as api_config
    import api.runtime_worker as runtime_worker
    import api.app as api_app

    reload(api_config)
    runtime_worker = reload(runtime_worker)
    api_app = reload(api_app)

    from service.task_executor import ComparePipelineRequest
    from service.task_executor import run_next_queued_task as real_run_next_queued_task

    def fake_pipeline(request: ComparePipelineRequest) -> Dict[str, Any]:
        return {
            "query_text": request.query_text,
            "detection_mode": request.detection_mode,
            "rewrite_detection": {
                "status": "fallback_lexical_only",
            },
            "fine": {
                "results": [
                    {
                        "book_name": "Smoke Novel",
                        "chapter_name": "Chapter 1",
                        "review_label": "high_risk",
                        "confidence_label": "strong",
                        "fine_score": 0.91,
                    }
                ],
                "review_rows": [
                    {
                        "detection_mode": request.detection_mode,
                        "fine_rank": 1,
                        "review_label": "high_risk",
                        "confidence_label": "strong",
                        "fine_score": 0.91,
                        "coarse_rank": 1,
                        "coarse_final_score": 0.83,
                        "dataset_key": "smoke_dataset",
                        "book_ext_id": "book-1",
                        "book_name": "Smoke Novel",
                        "chapter_uid": 1,
                        "chapter_ext_id": "chapter-1",
                        "chapter_name": "Chapter 1",
                        "candidate_window_order": 1,
                        "candidate_start_offset": 0,
                        "candidate_end_offset": 128,
                        "exact_substring_hit": True,
                        "longest_match_len": len(request.query_text),
                        "longest_match_ratio": 1.0,
                        "ngram_recall": 1.0,
                        "ngram_precision": 1.0,
                        "jaccard": 1.0,
                        "sequence_ratio": 1.0,
                        "matched_substring": request.query_text,
                        "query_text_preview": request.query_text,
                        "candidate_text_preview": request.query_text,
                        "query_text": request.query_text,
                        "candidate_text": request.query_text,
                    }
                ],
            },
        }

    def run_next_with_fake_pipeline(**kwargs: Any) -> Optional[Dict[str, Any]]:
        return real_run_next_queued_task(compare_fn=fake_pipeline, **kwargs)

    monkeypatch.setattr(runtime_worker, "run_next_queued_task", run_next_with_fake_pipeline)

    with TestClient(api_app.app) as client:
        create_response = client.post(
            "/api/v1/tasks",
            files={"file": ("batch.txt", b"first query\n\nsecond query", "text/plain")},
            data={"detection_mode": "rewrite"},
        )
        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]

        deadline = time.time() + 10
        while time.time() < deadline:
            detail_response = client.get(f"/api/v1/tasks/{task_id}")
            assert detail_response.status_code == 200
            task_payload = detail_response.json()
            if task_payload["task"]["status"] == "completed":
                break
            time.sleep(0.2)

        result_list_response = client.get(
            "/api/v1/results",
            params={"task_id": task_id, "status": "completed"},
        )
        assert result_list_response.status_code == 200
        listed_results = result_list_response.json()["items"]
        assert len(listed_results) == 2
        assert listed_results[0]["review"]["review_status"] == ""

        pending_response = client.get(
            "/api/v1/results",
            params={"task_id": task_id, "review_status": "pending"},
        )
        assert pending_response.status_code == 200
        pending_items = pending_response.json()["items"]
        assert len(pending_items) == 2

        result_id = listed_results[0]["result_id"]
        review_response = client.post(
            f"/api/v1/results/{result_id}/review",
            json={
                "review_status": "confirmed_high_risk",
                "reviewer_name": "pytest",
                "review_note": "smoke review note",
            },
        )
        assert review_response.status_code == 200
        reviewed_result = review_response.json()["result"]
        assert reviewed_result["review"]["review_status"] == "confirmed_high_risk"
        assert reviewed_result["review"]["reviewer_name"] == "pytest"
        assert reviewed_result["review"]["review_note"] == "smoke review note"

        filtered_response = client.get(
            "/api/v1/results",
            params={"task_id": task_id, "review_status": "confirmed_high_risk"},
        )
        assert filtered_response.status_code == 200
        filtered_items = filtered_response.json()["items"]
        assert len(filtered_items) == 1
        assert filtered_items[0]["result_id"] == result_id

        detail_response = client.get(f"/api/v1/results/{result_id}")
        assert detail_response.status_code == 200
        result_detail = detail_response.json()["result"]
        assert result_detail["review"]["review_status"] == "confirmed_high_risk"
        assert result_detail["review"]["reviewer_name"] == "pytest"

        pending_review_response = client.post(
            f"/api/v1/results/{listed_results[1]['result_id']}/review",
            json={
                "review_status": "pending",
                "reviewer_name": "pytest",
                "review_note": "kept pending",
            },
        )
        assert pending_review_response.status_code == 200

        pending_after_save_response = client.get(
            "/api/v1/results",
            params={"task_id": task_id, "review_status": "pending"},
        )
        assert pending_after_save_response.status_code == 200
        pending_after_save_items = pending_after_save_response.json()["items"]
        assert len(pending_after_save_items) == 1
        assert pending_after_save_items[0]["result_id"] == listed_results[1]["result_id"]


def test_api_cancel_queued_task_smoke(tmp_path: Path, monkeypatch) -> None:
    business_db = tmp_path / "web_business.sqlite3"
    retrieval_db = tmp_path / "retrieval.sqlite3"
    upload_root = tmp_path / "uploads"
    export_root = tmp_path / "exports"

    monkeypatch.setenv("NOVEL_SIMILARITY_AUTOSTART_WORKER", "0")
    monkeypatch.setenv("NOVEL_SIMILARITY_BUSINESS_DB", str(business_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_DB", str(retrieval_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_UPLOAD_ROOT", str(upload_root))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_EXPORT_ROOT", str(export_root))

    import api.config as api_config
    import api.runtime_worker as runtime_worker
    import api.app as api_app

    reload(api_config)
    reload(runtime_worker)
    api_app = reload(api_app)

    with TestClient(api_app.app) as client:
        create_response = client.post(
            "/api/v1/tasks",
            files={"file": ("batch.txt", b"first query\n\nsecond query", "text/plain")},
            data={"detection_mode": "rewrite"},
        )
        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]

        cancel_response = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert cancel_response.status_code == 200
        payload = cancel_response.json()
        assert payload["task"]["task_id"] == task_id
        assert payload["task"]["status"] == "cancelled"

        detail_response = client.get(f"/api/v1/tasks/{task_id}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["task"]["status"] == "cancelled"

        missing_export_response = client.get(f"/api/v1/tasks/{task_id}/exports/summary")
        assert missing_export_response.status_code == 404
        assert missing_export_response.json()["detail"] == "export file not found"


def test_api_retry_cancelled_task_smoke(tmp_path: Path, monkeypatch) -> None:
    business_db = tmp_path / "web_business.sqlite3"
    retrieval_db = tmp_path / "retrieval.sqlite3"
    upload_root = tmp_path / "uploads"
    export_root = tmp_path / "exports"

    monkeypatch.setenv("NOVEL_SIMILARITY_AUTOSTART_WORKER", "0")
    monkeypatch.setenv("NOVEL_SIMILARITY_BUSINESS_DB", str(business_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_DB", str(retrieval_db))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_UPLOAD_ROOT", str(upload_root))
    monkeypatch.setenv("NOVEL_SIMILARITY_TASK_EXPORT_ROOT", str(export_root))

    import api.config as api_config
    import api.runtime_worker as runtime_worker
    import api.app as api_app

    reload(api_config)
    reload(runtime_worker)
    api_app = reload(api_app)

    with TestClient(api_app.app) as client:
        create_response = client.post(
            "/api/v1/tasks",
            files={"file": ("batch.txt", b"first query\n\nsecond query", "text/plain")},
            data={"detection_mode": "rewrite"},
        )
        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]

        cancel_response = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert cancel_response.status_code == 200

        retry_response = client.post(f"/api/v1/tasks/{task_id}/retry")
        assert retry_response.status_code == 200
        retry_payload = retry_response.json()
        assert retry_payload["task_id"] != task_id
        assert retry_payload["status"] == "queued"
        assert retry_payload["detection_mode"] == "rewrite"

        new_detail_response = client.get(f"/api/v1/tasks/{retry_payload['task_id']}")
        assert new_detail_response.status_code == 200
        new_detail = new_detail_response.json()
        assert new_detail["task"]["status"] == "queued"
        assert new_detail["task"]["source_file_name"] == "batch.txt"
