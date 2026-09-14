from __future__ import annotations

from argparse import Namespace

import pytest

from scripts import execute_cover_staging_cleanup_v1 as cleanup_cli


class FakeStore:
    def __init__(self, plan: dict[str, object]) -> None:
        self.plan = plan

    def get_cleanup_plan(self, scope: object, cleanup_run_id: str) -> dict[str, object]:
        return self.plan


def _args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "db_path": "cover.sqlite3",
        "workspace_key": "internal",
        "user_id": 7,
        "cleanup_run_id": "cleanup-123",
        "staging_root": "staging",
        "confirm_cleanup_run_id": "cleanup-123",
        "max_items": 10,
        "max_bytes": 1_000,
        "execute": True,
    }
    values.update(overrides)
    return Namespace(**values)


def _plan(*sizes: int) -> dict[str, object]:
    return {
        "cleanup_run_id": "cleanup-123",
        "cleanup_kind": "staging",
        "status": "approved",
        "items": [
            {"item_key": f"video-{index}/cover.jpg", "byte_size": size}
            for index, size in enumerate(sizes, start=1)
        ],
    }


def test_cli_requires_execute_flag_before_loading_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup_cli, "CoverMonitorStore", lambda path: pytest.fail("store opened"))

    with pytest.raises(ValueError, match="--execute"):
        cleanup_cli.run(_args(execute=False), {"COVER_CLEANUP_EXECUTION_TOKEN": "secret"})


def test_cli_requires_exact_cleanup_run_id_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cleanup_cli, "CoverMonitorStore", lambda path: pytest.fail("store opened"))

    with pytest.raises(ValueError, match="confirmation"):
        cleanup_cli.run(
            _args(confirm_cleanup_run_id="cleanup-other"),
            {"COVER_CLEANUP_EXECUTION_TOKEN": "secret"},
        )


def test_cli_requires_execution_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup_cli, "CoverMonitorStore", lambda path: pytest.fail("store opened"))

    with pytest.raises(ValueError, match="COVER_CLEANUP_EXECUTION_TOKEN"):
        cleanup_cli.run(_args(), {})


@pytest.mark.parametrize(
    ("plan", "args", "message"),
    [
        (_plan(10, 20), _args(max_items=1), "item safety limit"),
        (_plan(600, 500), _args(max_bytes=1_000), "byte safety limit"),
    ],
)
def test_cli_rejects_plans_over_safety_limits_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    plan: dict[str, object],
    args: Namespace,
    message: str,
) -> None:
    monkeypatch.setattr(cleanup_cli, "CoverMonitorStore", lambda path: FakeStore(plan))
    monkeypatch.setattr(
        cleanup_cli,
        "execute_staging_cleanup",
        lambda *positional, **keywords: pytest.fail("execution token consumed"),
    )

    with pytest.raises(ValueError, match=message):
        cleanup_cli.run(args, {"COVER_CLEANUP_EXECUTION_TOKEN": "secret"})


def test_cli_executes_an_approved_plan_without_exposing_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(100, 200)
    captured: dict[str, object] = {}

    def fake_execute(*positional: object, **keywords: object) -> object:
        captured.update(keywords)
        return cleanup_cli.StagingCleanupExecutionResult(
            cleanup_run_id="cleanup-123",
            status="completed",
            deleted_count=2,
            skipped_count=0,
            failed_count=0,
        )

    monkeypatch.setattr(cleanup_cli, "CoverMonitorStore", lambda path: FakeStore(plan))
    monkeypatch.setattr(cleanup_cli, "execute_staging_cleanup", fake_execute)

    report = cleanup_cli.run(
        _args(),
        {"COVER_CLEANUP_EXECUTION_TOKEN": "one-time-secret"},
    )

    assert captured["execution_token"] == "one-time-secret"
    assert report == {
        "cleanup_run_id": "cleanup-123",
        "status": "completed",
        "candidate_count": 2,
        "candidate_bytes": 300,
        "deleted_count": 2,
        "skipped_count": 0,
        "failed_count": 0,
    }
    assert "one-time-secret" not in repr(report)
