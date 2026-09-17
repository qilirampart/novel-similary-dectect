from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from openpyxl import Workbook

from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


HEADERS = [
    "代理商简称",
    "频道 ID",
    "频道名",
    "视频标题",
    "原视频链接",
    "视频 ID",
    "封面 CDN 地址",
    "检测结论",
    "风险标签",
    "摘要",
    "可见证据",
    "置信度",
]


def _write_workbook(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "全部合并"
    worksheet.append(HEADERS)
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def _row(operator: str, channel_id: str, video_id: str, risk: str = "safe") -> list[object]:
    return [
        operator,
        channel_id,
        f"频道-{channel_id}",
        f"视频-{video_id}",
        f"https://www.youtube.com/watch?v={video_id}",
        video_id,
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        risk,
        "校园;未成年人" if risk == "risk" else "",
        "历史摘要",
        "历史可见证据",
        "95%",
    ]


def test_preview_reports_duplicate_conflict_and_missing_rows(tmp_path: Path) -> None:
    source = tmp_path / "baseline.xlsx"
    missing = _row("代理甲", "UC-3", "video-3")
    missing[6] = ""
    _write_workbook(
        source,
        [
            _row("代理甲", "UC-1", "video-1"),
            _row("代理甲", "UC-1", "video-1"),
            _row("代理乙", "UC-2", "video-1"),
            missing,
        ],
    )
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)

    preview = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=source.name,
        source_file_sha256="sha-preview",
        source_file_path=str(source),
    )

    assert preview["status"] == "previewed"
    assert preview["sheet_name"] == "全部合并"
    assert preview["stats"] == {
        "total_rows": 4,
        "valid_rows": 1,
        "unique_channels": 3,
        "unique_videos": 2,
        "duplicate_rows": 1,
        "conflict_rows": 1,
        "missing_rows": 1,
        "operator_conflicts": 0,
    }
    assert preview["conflict_samples"][0]["conflict_type"] == "video_channel_mismatch"
    assert store.get_overview(scope)["channel_count"] == 0


def test_headerless_single_column_channel_urls_are_imported_without_dropping_first_row(
    tmp_path: Path,
) -> None:
    source = tmp_path / "headerless-channels.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["https://www.youtube.com/channel/UCX8oe3DG5lg7FYWguvuGW0Q"])
    worksheet.append(["https://www.youtube.com/channel/UCcO2KUInJzw53A5qJj9BNhg"])
    workbook.save(source)
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)

    preview = store.create_import_preview(
        scope,
        import_kind="channels",
        source_file_name=source.name,
        source_file_sha256="headerless-channels",
        source_file_path=str(source),
    )

    assert preview["mapping"] == {"channel_url": "频道链接"}
    assert preview["stats"]["total_rows"] == 2
    assert preview["stats"]["valid_rows"] == 2
    assert preview["stats"]["unique_channels"] == 2
    confirmed = store.confirm_import(scope, preview["import_id"])
    assert confirmed["stats"]["applied_channels"] == 2
    channels = store.search_channels(scope, limit=10, offset=0)["items"]
    assert {item["name"] for item in channels} == {
        "UCX8oe3DG5lg7FYWguvuGW0Q",
        "UCcO2KUInJzw53A5qJj9BNhg",
    }


def test_confirm_import_is_idempotent_and_preserves_legacy_observation(tmp_path: Path) -> None:
    source = tmp_path / "baseline.xlsx"
    _write_workbook(source, [_row("代理甲", "UC-1", "video-1", "risk")])
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    preview = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=source.name,
        source_file_sha256="sha-confirm",
        source_file_path=str(source),
    )

    first = store.confirm_import(scope, preview["import_id"])
    second = store.confirm_import(scope, preview["import_id"])

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert first["stats"]["applied_channels"] == 1
    assert first["stats"]["applied_videos"] == 1
    assert first["stats"]["historical_observations"] == 1
    assert store.get_overview(scope)["video_count"] == 1
    with sqlite3.connect(store.path) as conn:
        observation = conn.execute(
            "SELECT overall_risk, risk_tags_json, evidence_status, model_version FROM cover_historical_observations"
        ).fetchone()
        assert observation == (
            "risk",
            json.dumps(["校园", "未成年人"], ensure_ascii=False),
            "evidence_missing",
            "legacy_unknown",
        )
        assert conn.execute("SELECT COUNT(*) FROM cover_videos").fetchone()[0] == 1


def test_historical_baseline_controls_incremental_scan_reasons(tmp_path: Path) -> None:
    source = tmp_path / "baseline.xlsx"
    _write_workbook(
        source,
        [
            _row("代理甲", "UC-1", "video-safe", "safe"),
            _row("代理甲", "UC-1", "video-risk", "risk"),
            _row("代理甲", "UC-1", "video-review", "review"),
            _row("代理甲", "UC-1", "video-unknown", "unknown"),
        ],
    )
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    preview = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=source.name,
        source_file_sha256="sha-scan-reasons",
        source_file_path=str(source),
    )
    store.confirm_import(scope, preview["import_id"])

    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        videos = [
            dict(row)
            for row in conn.execute(
                "SELECT video_pk, video_id FROM cover_videos ORDER BY video_id"
            )
        ]
    reasons = {
        video["video_id"]: store.get_video_scan_reason(scope, video["video_pk"])
        for video in videos
    }

    assert reasons == {
        "video-safe": None,
        "video-risk": "historical_risk",
        "video-review": "historical_risk",
        "video-unknown": "retry_unknown",
    }

    confirmed_risks = store.list_risk_cases(
        scope,
        status="confirmed_risk",
        limit=10,
        offset=0,
    )
    assert confirmed_risks["total"] == 1
    assert confirmed_risks["items"][0]["video_id"] == "video-risk"
    assert confirmed_risks["items"][0]["current_status"] == "confirmed_risk"
    assert confirmed_risks["items"][0]["opened_summary"] == "历史摘要"
    assert store.list_risk_cases(scope, status="needs_review")["total"] == 0
    assert store.list_risk_cases(scope)["total"] == 1

    results = store.list_results(scope, limit=10, offset=0)
    assert results["counts"] == {
        "all": 3,
        "risk": 1,
        "review": 1,
        "unknown": 1,
    }
    assert {item["video_id"] for item in results["items"]} == {
        "video-risk",
        "video-review",
        "video-unknown",
    }
    assert {item["source"] for item in results["items"]} == {"historical_import"}
    assert store.list_results(scope, overall_risk="risk")["total"] == 1
    overview = store.get_overview(scope)
    assert overview["risk_distribution"] == {
        "safe": 1,
        "review": 1,
        "risk": 1,
        "unknown": 1,
    }
    assert overview["risk_count"] == 1
    assert overview["pending_review_count"] == 2

    historical_case = store.get_risk_case_detail(
        scope,
        confirmed_risks["items"][0]["case_id"],
    )
    assert historical_case is not None
    assert historical_case["case"]["opened_risk"] == "risk"
    assert historical_case["events"][0]["event_type"] == "historical_risk_imported"
    assert historical_case["events"][0]["summary"] == "历史摘要"
    assert historical_case["events"][0]["original_url"].endswith(
        "/video-risk/hqdefault.jpg"
    )
    assert historical_case["reviews"] == []


def test_historical_results_and_cases_filter_by_operator_then_channel(tmp_path: Path) -> None:
    source = tmp_path / "baseline-scope.xlsx"
    _write_workbook(
        source,
        [
            _row("代理甲", "UC-A1", "video-a1", "risk"),
            _row("代理甲", "UC-A2", "video-a2", "review"),
            _row("代理乙", "UC-B1", "video-b1", "risk"),
        ],
    )
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    preview = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=source.name,
        source_file_sha256="sha-result-scope",
        source_file_path=str(source),
    )
    store.confirm_import(scope, preview["import_id"])

    options = store.list_filter_options(scope)
    operator_by_name = {item["name"]: item for item in options["operators"]}
    operator_a = operator_by_name["代理甲"]
    operator_b = operator_by_name["代理乙"]
    assert operator_a["channel_count"] == 2
    assert operator_b["channel_count"] == 1
    assert options["channels"] == []

    operator_options = store.list_filter_options(
        scope,
        operator_pk=operator_a["operator_pk"],
    )
    assert {item["channel_id"] for item in operator_options["channels"]} == {
        "UC-A1",
        "UC-A2",
    }
    channel_a1 = next(
        item for item in operator_options["channels"] if item["channel_id"] == "UC-A1"
    )

    operator_results = store.list_results(
        scope,
        operator_pk=operator_a["operator_pk"],
    )
    assert operator_results["counts"] == {
        "all": 2,
        "risk": 1,
        "review": 1,
        "unknown": 0,
    }
    channel_results = store.list_results(
        scope,
        operator_pk=operator_a["operator_pk"],
        channel_pk=channel_a1["channel_pk"],
    )
    assert channel_results["total"] == 1
    assert channel_results["items"][0]["video_id"] == "video-a1"
    operator_cases = store.list_risk_cases(
        scope,
        status="confirmed_risk",
        operator_pk=operator_b["operator_pk"],
    )
    assert operator_cases["total"] == 1
    assert operator_cases["items"][0]["video_id"] == "video-b1"


def test_same_file_hash_returns_existing_preview_without_duplicate_rows(tmp_path: Path) -> None:
    source = tmp_path / "channels.xlsx"
    _write_workbook(source, [_row("代理甲", "UC-1", "video-1")])
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    args = {
        "import_kind": "baseline",
        "source_file_name": source.name,
        "source_file_sha256": "same-sha",
        "source_file_path": str(source),
    }

    first = store.create_import_preview(scope, **args)
    second = store.create_import_preview(scope, **args)

    assert first["import_id"] == second["import_id"]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_import_batches").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cover_import_rows").fetchone()[0] == 1


def test_preview_detects_conflict_against_existing_database(tmp_path: Path) -> None:
    first_source = tmp_path / "first.xlsx"
    second_source = tmp_path / "second.xlsx"
    _write_workbook(first_source, [_row("代理甲", "UC-1", "video-1")])
    _write_workbook(
        second_source,
        [
            _row("代理乙", "UC-2", "video-1"),
            _row("代理乙", "UC-1", "video-2"),
            _row("代理乙", "UC-1", "video-3"),
        ],
    )
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    first = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=first_source.name,
        source_file_sha256="first-sha",
        source_file_path=str(first_source),
    )
    store.confirm_import(scope, first["import_id"])

    second = store.create_import_preview(
        scope,
        import_kind="baseline",
        source_file_name=second_source.name,
        source_file_sha256="second-sha",
        source_file_path=str(second_source),
    )

    assert second["stats"]["conflict_rows"] == 3
    assert second["stats"]["valid_rows"] == 0
    assert second["conflict_samples"][0]["conflict_type"] == "video_channel_mismatch"
