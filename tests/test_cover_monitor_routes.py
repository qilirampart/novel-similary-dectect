from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from api.cover_routes import build_cover_monitor_router
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


def test_cover_overview_uses_independent_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "cover-monitor.sqlite3"
    app = FastAPI()
    app.include_router(
        build_cover_monitor_router(
            db_path=str(db_path),
            current_user_dependency=lambda: {"user_id": 7, "role": "operator"},
        )
    )

    response = TestClient(app).get("/api/v1/cover-monitor/overview")

    assert response.status_code == 200
    assert response.json() == {
        "channel_count": 0,
        "video_count": 0,
        "risk_count": 0,
        "pending_review_count": 0,
        "risk_distribution": {"safe": 0, "review": 0, "risk": 0, "unknown": 0},
        "latest_run": None,
    }
    assert db_path.is_file()


def _channel_workbook(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["代理商简称", "频道 ID", "频道名", "频道链接"])
    worksheet.append([
        "代理甲",
        "UC-route-1",
        "接口测试频道",
        "https://www.youtube.com/channel/UC-route-1",
    ])
    workbook.save(path)


def test_cover_import_preview_and_confirm_routes(tmp_path: Path) -> None:
    db_path = tmp_path / "cover-monitor.sqlite3"
    import_root = tmp_path / "imports"
    source = tmp_path / "channels.xlsx"
    _channel_workbook(source)
    app = FastAPI()
    app.include_router(
        build_cover_monitor_router(
            db_path=str(db_path),
            import_root=str(import_root),
            current_user_dependency=lambda: {"user_id": 7, "role": "operator"},
        )
    )
    client = TestClient(app)

    with source.open("rb") as upload:
        preview_response = client.post(
            "/api/v1/cover-monitor/imports/preview",
            data={"import_kind": "channels"},
            files={"file": (source.name, upload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["status"] == "previewed"
    assert preview["stats"]["unique_channels"] == 1
    confirm_response = client.post(
        f"/api/v1/cover-monitor/imports/{preview['import_id']}/confirm"
    )
    assert confirm_response.status_code == 200
    assert confirm_response.json()["stats"]["applied_channels"] == 1
    assert client.get("/api/v1/cover-monitor/overview").json()["channel_count"] == 1


def test_cover_import_rejects_non_xlsx_and_oversized_files(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(
        build_cover_monitor_router(
            db_path=str(tmp_path / "cover.sqlite3"),
            import_root=str(tmp_path / "imports"),
            import_max_bytes=4,
            current_user_dependency=lambda: {"user_id": 7},
        )
    )
    client = TestClient(app)

    invalid = client.post(
        "/api/v1/cover-monitor/imports/preview",
        files={"file": ("channels.csv", b"a,b", "text/csv")},
    )
    oversized = client.post(
        "/api/v1/cover-monitor/imports/preview",
        files={"file": ("channels.xlsx", b"12345", "application/octet-stream")},
    )

    assert invalid.status_code == 400
    assert oversized.status_code == 413


def test_cover_import_rejects_corrupt_xlsx_as_client_error(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(
        build_cover_monitor_router(
            db_path=str(tmp_path / "cover.sqlite3"),
            import_root=str(tmp_path / "imports"),
            current_user_dependency=lambda: {"user_id": 7},
        )
    )

    response = TestClient(app).post(
        "/api/v1/cover-monitor/imports/preview",
        files={"file": ("broken.xlsx", b"not-an-xlsx", "application/octet-stream")},
    )

    assert response.status_code == 400


def _cover_run_client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "cover.sqlite3"
    app = FastAPI()
    app.include_router(
        build_cover_monitor_router(
            db_path=str(db_path),
            current_user_dependency=lambda: {"user_id": 7, "role": "operator"},
            vision_provider="vision.example.test",
            vision_model="test-vision-model",
        )
    )
    client = TestClient(app)
    store = CoverMonitorStore(db_path)
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    for index in range(1, 3):
        store.upsert_channel(
            scope,
            platform="youtube",
            channel_id=f"UC-route-run-{index}",
            name=f"巡检频道 {index}",
            source_url=f"https://www.youtube.com/channel/UC-route-run-{index}",
        )
    return client, db_path


def test_cover_run_routes_create_list_and_get_without_persisting_secrets(tmp_path: Path) -> None:
    client, db_path = _cover_run_client(tmp_path)

    created_response = client.post(
        "/api/v1/cover-monitor/runs",
        json={
            "intensity": "strict",
            "include_shorts": False,
            "force_refresh": True,
            "max_items_per_scope": 25,
        },
    )

    assert created_response.status_code == 200
    created = created_response.json()
    assert created["status"] == "queued"
    assert created["intensity"] == "strict"
    assert created["total_channel_count"] == 2
    listed = client.get("/api/v1/cover-monitor/runs?limit=10&offset=0")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["run_id"] == created["run_id"]
    detail = client.get(f"/api/v1/cover-monitor/runs/{created['run_id']}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == created["run_id"]

    with sqlite3.connect(db_path) as conn:
        snapshot = conn.execute(
            "SELECT model_snapshot_json FROM cover_runs WHERE run_id = ?",
            (created["run_id"],),
        ).fetchone()[0]
    assert "test-vision-model" in snapshot
    assert "api_key" not in snapshot.casefold()


def test_cover_run_routes_pause_resume_and_cancel_with_state_conflicts(tmp_path: Path) -> None:
    client, _ = _cover_run_client(tmp_path)
    created = client.post("/api/v1/cover-monitor/runs", json={}).json()
    run_url = f"/api/v1/cover-monitor/runs/{created['run_id']}"

    paused = client.post(f"{run_url}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    resumed = client.post(f"{run_url}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "queued"
    cancelled = client.post(f"{run_url}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    invalid_resume = client.post(f"{run_url}/resume")
    assert invalid_resume.status_code == 409
    assert client.get("/api/v1/cover-monitor/runs/missing-run").status_code == 404


def test_cover_run_create_validates_channel_scope_and_request_limits(tmp_path: Path) -> None:
    client, _ = _cover_run_client(tmp_path)

    missing_channel = client.post(
        "/api/v1/cover-monitor/runs",
        json={"channel_pks": [999999]},
    )
    invalid_limit = client.post(
        "/api/v1/cover-monitor/runs",
        json={"max_items_per_scope": 10001},
    )

    assert missing_channel.status_code == 409
    assert invalid_limit.status_code == 422


def test_cover_run_detail_returns_channel_progress_and_paginated_items(tmp_path: Path) -> None:
    client, db_path = _cover_run_client(tmp_path)
    created = client.post("/api/v1/cover-monitor/runs", json={}).json()
    store = CoverMonitorStore(db_path)
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.list_channels(scope)[0]
    video = store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-route-detail",
        title="Detail Video",
        video_url="https://www.youtube.com/watch?v=video-route-detail",
        thumbnail_url="https://i.ytimg.com/vi/video-route-detail/hqdefault.jpg",
    )
    claimed = store.claim_next_run(worker_name="route-detail-worker")
    assert claimed is not None
    store.enqueue_task_items(
        created["run_id"],
        claimed["worker_lease_token"],
        [{"video_pk": video["video_pk"], "reason": "manual"}],
    )

    response = client.get(
        f"/api/v1/cover-monitor/runs/{created['run_id']}/detail?item_limit=1&item_offset=0"
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail["run"]["run_id"] == created["run_id"]
    assert detail["item_total"] == 1
    assert detail["item_limit"] == 1
    assert detail["channels"][0]["channel_name"] == "巡检频道 1"
    assert detail["items"][0]["video_id"] == "video-route-detail"
    assert detail["items"][0]["reason"] == "manual"
    assert client.get("/api/v1/cover-monitor/runs/missing/detail").status_code == 404
