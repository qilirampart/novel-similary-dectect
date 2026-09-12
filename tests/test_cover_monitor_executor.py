from __future__ import annotations

from pathlib import Path

from service.cover_monitor.collector import (
    ChannelCollectionResult,
    CollectedCoverVideo,
    CoverCollectionCancelled,
)
from service.cover_monitor.downloader import DownloadedCover
from service.cover_monitor.executor import CoverRunExecutor
from service.cover_monitor.reviewer import CoverDetectionResult, CoverReviewOutcome
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


class FakeCollector:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, source_url: str, **_: object) -> ChannelCollectionResult:
        self.calls += 1
        return ChannelCollectionResult(
            source_url=source_url,
            channel_id="UC-executor",
            channel_name="Executor Channel",
            videos=(
                CollectedCoverVideo(
                    video_id="video-executor-001",
                    title="Potentially risky cover",
                    video_url="https://www.youtube.com/watch?v=video-executor-001",
                    thumbnail_url="https://i.ytimg.com/vi/video-executor-001/hqdefault.jpg",
                    channel_id="UC-executor",
                    channel_name="Executor Channel",
                    upload_date="20260912",
                ),
            ),
            scopes_completed=("videos", "shorts"),
            scope_item_counts={"videos": 1, "shorts": 0},
            scope_errors={},
        )


class FakeDownloader:
    def __init__(self, root: Path) -> None:
        self.root = root

    def download(self, video_id: str, original_url: str = "") -> DownloadedCover:
        storage_key = f"{video_id}/fake.jpg"
        path = self.root / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-image")
        return DownloadedCover(
            video_id=video_id,
            original_url=original_url,
            fetched_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            local_path=str(path),
            storage_key=storage_key,
            content_sha256="a" * 64,
            mime_type="image/jpeg",
            byte_size=10,
            width=1280,
            height=720,
        )


class FakeReviewer:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls = 0

    def review(self, **_: object) -> CoverReviewOutcome:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            return CoverReviewOutcome(
                status="failed",
                error_type="timeout",
                error_message="provider timed out",
                duration_seconds=1.0,
            )
        return CoverReviewOutcome(
            status="succeeded",
            provider_status=200,
            duration_seconds=0.2,
            result=CoverDetectionResult(
                overall_risk="risk",
                risk_tags=("未成年人",),
                summary="封面包含疑似未成年人风险元素",
                evidence="画面主体呈现学生制服与校园文字",
                confidence=0.91,
                provider="fake-provider",
                model="fake-model",
                prompt_version="cover-visible-evidence-v1",
                prompt_hash="b" * 64,
                intensity="standard",
                raw_response='{"overall_risk":"risk"}',
                duration_seconds=0.2,
            ),
        )


class PauseDuringCollection:
    def __init__(self, store: CoverMonitorStore, scope: CoverAccessScope, run_id: str) -> None:
        self.store = store
        self.scope = scope
        self.run_id = run_id

    def collect(self, _source_url: str, **kwargs: object) -> ChannelCollectionResult:
        self.store.request_pause(self.scope, self.run_id)
        should_cancel = kwargs["should_cancel"]
        if callable(should_cancel) and should_cancel():
            raise CoverCollectionCancelled("pause requested")
        raise AssertionError("pause request was not visible to the collector")


class CancelDuringReview(FakeReviewer):
    def __init__(self, store: CoverMonitorStore, scope: CoverAccessScope, run_id: str) -> None:
        super().__init__()
        self.store = store
        self.scope = scope
        self.run_id = run_id

    def review(self, **kwargs: object) -> CoverReviewOutcome:
        self.store.request_cancel(self.scope, self.run_id)
        return super().review(**kwargs)


def _create_executor_fixture(
    tmp_path: Path,
    *,
    fail_once: bool = False,
    retry_delay_seconds: float = 0,
):
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-executor",
        name="Executor Channel",
        source_url="https://www.youtube.com/channel/UC-executor",
    )
    run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"include_shorts": True, "max_items_per_scope": 10},
        model_snapshot={"provider": "fake-provider", "model": "fake-model"},
        prompt_version="cover-visible-evidence-v1",
    )
    reviewer = FakeReviewer(fail_once=fail_once)
    executor = CoverRunExecutor(
        store=store,
        collector=FakeCollector(),
        downloader=FakeDownloader(tmp_path / "assets"),
        reviewer=reviewer,
        worker_name="cover-worker-test",
        max_attempts=2,
        retry_delay_seconds=retry_delay_seconds,
    )
    return store, scope, run, reviewer, executor


def test_cover_executor_persists_asset_detection_and_risk_case(tmp_path: Path) -> None:
    store, scope, run, reviewer, executor = _create_executor_fixture(tmp_path)

    result = executor.run_once()

    assert result is not None
    assert result["run_id"] == run["run_id"]
    finished = store.get_run(scope, run["run_id"])
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["total_item_count"] == 1
    assert finished["completed_item_count"] == 1
    assert finished["failed_item_count"] == 0
    assert reviewer.calls == 1
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_assets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cover_detections").fetchone()[0] == 1
        risk_case = conn.execute("SELECT current_status FROM cover_risk_cases").fetchone()
        assert risk_case is not None
        assert risk_case["current_status"] == "needs_review"
        run_channel = conn.execute(
            "SELECT scan_status, completeness, discovered_count FROM cover_run_channels"
        ).fetchone()
        assert tuple(run_channel) == ("completed", "complete", 1)


def test_cover_executor_retries_transient_review_failure_without_duplicate_asset(tmp_path: Path) -> None:
    store, scope, run, reviewer, executor = _create_executor_fixture(
        tmp_path,
        fail_once=True,
        retry_delay_seconds=0.05,
    )

    executor.run_once()

    finished = store.get_run(scope, run["run_id"])
    assert finished is not None
    assert finished["status"] == "completed"
    assert finished["completed_item_count"] == 1
    assert reviewer.calls == 2
    with store._connect() as conn:
        item = conn.execute("SELECT attempts, status FROM cover_task_items").fetchone()
        assert tuple(item) == (2, "succeeded")
        assert conn.execute("SELECT COUNT(*) FROM cover_assets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cover_attempts").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM cover_detections").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cover_risk_cases").fetchone()[0] == 1


def test_cover_executor_recovery_finishes_persist_stage_without_recalling_model(tmp_path: Path) -> None:
    store, scope, run, reviewer, executor = _create_executor_fixture(tmp_path)
    executor.run_once()
    assert reviewer.calls == 1

    with store._connect() as conn:
        conn.execute(
            """
            UPDATE cover_runs
               SET status = 'queued', worker_name = NULL, worker_lease_token = NULL,
                   last_heartbeat_at = NULL, finished_at = NULL,
                   completed_item_count = 0
             WHERE run_id = ?
            """,
            (run["run_id"],),
        )
        conn.execute(
            """
            UPDATE cover_task_items
               SET status = 'queued', stage = 'persist', worker_lease_token = NULL,
                   last_heartbeat_at = NULL, finished_at = NULL
             WHERE run_id = ?
            """,
            (run["run_id"],),
        )
    reviewer.calls = 0

    recovered = executor.run_once()

    assert recovered is not None
    assert recovered["status"] == "completed"
    assert reviewer.calls == 0
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_detections").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cover_risk_cases").fetchone()[0] == 1


def test_cover_executor_releases_run_before_scan_when_worker_is_stopping(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-stop",
        name="Stop Channel",
        source_url="https://www.youtube.com/channel/UC-stop",
    )
    run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    collector = FakeCollector()
    executor = CoverRunExecutor(
        store=store,
        collector=collector,
        downloader=FakeDownloader(tmp_path / "assets"),
        reviewer=FakeReviewer(),
        worker_name="cover-worker-stopping",
        should_stop=lambda: True,
    )

    result = executor.run_once()

    assert result is not None
    assert result["run_id"] == run["run_id"]
    assert result["status"] == "queued"
    assert result["worker_lease_token"] is None
    assert collector.calls == 0


def test_cover_executor_pauses_during_channel_collection_at_safe_checkpoint(tmp_path: Path) -> None:
    store, scope, run, _, _ = _create_executor_fixture(tmp_path)
    executor = CoverRunExecutor(
        store=store,
        collector=PauseDuringCollection(store, scope, run["run_id"]),
        downloader=FakeDownloader(tmp_path / "assets"),
        reviewer=FakeReviewer(),
        worker_name="cover-worker-pausing",
    )

    result = executor.run_once()

    assert result is not None
    assert result["status"] == "paused"
    assert result["worker_lease_token"] is None
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_task_items").fetchone()[0] == 0
        assert conn.execute("SELECT scan_status FROM cover_run_channels").fetchone()[0] == "pending"


def test_cover_executor_cancel_during_review_keeps_completed_evidence_and_cancels_run(tmp_path: Path) -> None:
    store, scope, run, _, _ = _create_executor_fixture(tmp_path)
    reviewer = CancelDuringReview(store, scope, run["run_id"])
    executor = CoverRunExecutor(
        store=store,
        collector=FakeCollector(),
        downloader=FakeDownloader(tmp_path / "assets"),
        reviewer=reviewer,
        worker_name="cover-worker-cancelling",
    )

    result = executor.run_once()

    assert result is not None
    assert result["status"] == "cancelled"
    assert result["worker_lease_token"] is None
    assert reviewer.calls == 1
    with store._connect() as conn:
        assert conn.execute("SELECT status FROM cover_task_items").fetchone()[0] == "succeeded"
        assert conn.execute("SELECT COUNT(*) FROM cover_detections").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cover_risk_cases").fetchone()[0] == 1


def test_default_scan_processes_preimported_video_without_current_detection(tmp_path: Path) -> None:
    store = CoverMonitorStore(tmp_path / "cover.sqlite3")
    scope = CoverAccessScope(workspace_key="internal", user_id=7)
    channel = store.upsert_channel(
        scope,
        platform="youtube",
        channel_id="UC-executor",
        name="Executor Channel",
        source_url="https://www.youtube.com/channel/UC-executor",
    )
    store.upsert_video(
        scope,
        channel_pk=channel["channel_pk"],
        platform="youtube",
        video_id="video-executor-001",
        title="Imported before first inspection",
        video_url="https://www.youtube.com/watch?v=video-executor-001",
        thumbnail_url="https://i.ytimg.com/vi/video-executor-001/hqdefault.jpg",
    )
    run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": False},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    reviewer = FakeReviewer()
    executor = CoverRunExecutor(
        store=store,
        collector=FakeCollector(),
        downloader=FakeDownloader(tmp_path / "assets"),
        reviewer=reviewer,
        worker_name="cover-worker-initial-scan",
    )

    result = executor.run_once()

    assert result is not None and result["status"] == "completed"
    assert result["total_item_count"] == 1
    assert reviewer.calls == 1
    with store._connect() as conn:
        reason = conn.execute(
            "SELECT reason FROM cover_task_items WHERE run_id = ?",
            (run["run_id"],),
        ).fetchone()[0]
    assert reason == "new_video"


def test_incremental_scan_skips_valid_detection_but_retries_latest_unknown(tmp_path: Path) -> None:
    store, scope, first_run, reviewer, executor = _create_executor_fixture(tmp_path)
    first_result = executor.run_once()
    assert first_result is not None and first_result["status"] == "completed"
    channel = store.list_channels(scope)[0]

    second_run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": False},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    second_result = executor.run_once()
    assert second_result is not None and second_result["status"] == "completed"
    assert second_result["total_item_count"] == 0
    assert reviewer.calls == 1

    with store._connect() as conn:
        conn.execute(
            "UPDATE cover_detections SET overall_risk = 'unknown' WHERE run_id = ?",
            (first_run["run_id"],),
        )
    third_run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": False},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    third_result = executor.run_once()

    assert third_result is not None and third_result["status"] == "completed"
    assert third_result["total_item_count"] == 1
    assert reviewer.calls == 2
    with store._connect() as conn:
        reason = conn.execute(
            "SELECT reason FROM cover_task_items WHERE run_id = ?",
            (third_run["run_id"],),
        ).fetchone()[0]
    assert reason == "retry_unknown"

    fourth_run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": True},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    fourth_result = executor.run_once()

    assert fourth_result is not None and fourth_result["total_item_count"] == 1
    assert reviewer.calls == 3
    with store._connect() as conn:
        reason = conn.execute(
            "SELECT reason FROM cover_task_items WHERE run_id = ?",
            (fourth_run["run_id"],),
        ).fetchone()[0]
    assert reason == "manual"
