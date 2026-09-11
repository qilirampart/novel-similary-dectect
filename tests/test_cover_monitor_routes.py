from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from api.cover_routes import build_cover_monitor_router


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
