from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import Response


def test_health_aliases_are_dependency_free(monkeypatch) -> None:
    import api.app as api_app

    monkeypatch.setattr(
        api_app,
        "SETTINGS",
        SimpleNamespace(
            business_db_path="missing-business.sqlite3",
            db_path="missing-retrieval.sqlite3",
            drama_subtitle_db_path="missing-drama.sqlite3",
        ),
    )

    legacy = asyncio.run(api_app.health())
    live = asyncio.run(api_app.health_live())

    assert legacy.status == "ok"
    assert live.status == "ok"
    assert live.service == legacy.service


def test_readiness_reports_missing_required_runtime_files(monkeypatch, tmp_path: Path) -> None:
    import api.app as api_app

    business_db = tmp_path / "business.sqlite3"
    retrieval_db = tmp_path / "retrieval.sqlite3"
    drama_db = tmp_path / "drama.sqlite3"
    business_db.touch()
    retrieval_db.touch()
    monkeypatch.setattr(
        api_app,
        "SETTINGS",
        SimpleNamespace(
            business_db_path=str(business_db),
            db_path=str(retrieval_db),
            drama_subtitle_db_path=str(drama_db),
        ),
    )

    response = Response()
    readiness = asyncio.run(api_app.health_ready(response))

    assert response.status_code == 200
    assert readiness.status == "ready"
    assert readiness.checks == {
        "business_db": "ready",
        "novel_retrieval_db": "ready",
        "drama_subtitle_db": "missing",
    }


def test_readiness_returns_not_ready_when_required_file_is_missing(monkeypatch, tmp_path: Path) -> None:
    import api.app as api_app

    retrieval_db = tmp_path / "retrieval.sqlite3"
    retrieval_db.touch()
    monkeypatch.setattr(
        api_app,
        "SETTINGS",
        SimpleNamespace(
            business_db_path=str(tmp_path / "missing-business.sqlite3"),
            db_path=str(retrieval_db),
            drama_subtitle_db_path=str(tmp_path / "missing-drama.sqlite3"),
        ),
    )

    response = Response()
    readiness = asyncio.run(api_app.health_ready(response))

    assert response.status_code == 503
    assert readiness.status == "not_ready"
    assert readiness.checks["business_db"] == "missing"
    assert readiness.checks["novel_retrieval_db"] == "ready"


def test_watchdog_defaults_to_liveness_and_records_readiness() -> None:
    from scripts.install_cloud_health_watchdog_v1 import (
        DEFAULT_LIVENESS_URL,
        DEFAULT_READINESS_URL,
        build_watchdog_script,
    )

    args = SimpleNamespace(
        api_service_name="novel-similarity-api.service",
        health_url=DEFAULT_LIVENESS_URL,
        readiness_url=DEFAULT_READINESS_URL,
        listen_port=18101,
        state_dir="/tmp/novel-similarity-watchdog",
        fail_threshold=3,
        health_timeout_seconds=8,
        recovery_wait_seconds=40,
    )

    script = build_watchdog_script(args)

    assert f'HEALTH_URL="{DEFAULT_LIVENESS_URL}"' in script
    assert f'READINESS_URL="{DEFAULT_READINESS_URL}"' in script
    assert 'FAIL_THRESHOLD="3"' in script
    assert "readiness_check()" in script
    assert "readiness_state=" in script
