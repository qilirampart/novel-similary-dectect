from __future__ import annotations

from pathlib import Path

import pytest

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
    def __init__(self, video_id: str = "video-executor-001") -> None:
        self.calls = 0
        self.video_id = video_id

    def collect(self, source_url: str, **_: object) -> ChannelCollectionResult:
        self.calls += 1
        return ChannelCollectionResult(
            source_url=source_url,
            channel_id="UC-executor",
            channel_name="Executor Channel",
            videos=(
                CollectedCoverVideo(
                    video_id=self.video_id,
                    title="Potentially risky cover",
                    video_url=f"https://www.youtube.com/watch?v={self.video_id}",
                    thumbnail_url=f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg",
                    channel_id="UC-executor",
                    channel_name="Executor Channel",
                    upload_date="20260912",
                ),
            ),
            scopes_completed=("videos", "shorts"),
            scope_item_counts={"videos": 1, "shorts": 0},
            scope_errors={},
        )


class PartialCollector(FakeCollector):
    def __init__(self, *, include_video: bool = True) -> None:
        super().__init__()
        self.include_video = include_video

    def collect(self, source_url: str, **kwargs: object) -> ChannelCollectionResult:
        complete = super().collect(source_url, **kwargs)
        return ChannelCollectionResult(
            source_url=complete.source_url,
            channel_id=complete.channel_id,
            channel_name=complete.channel_name,
            videos=complete.videos if self.include_video else (),
            scopes_completed=("videos",),
            scope_item_counts={"videos": len(complete.videos) if self.include_video else 0},
            scope_errors={"shorts": "Shorts 采集超时"},
        )


class FakeDownloader:
    def __init__(self, root: Path, *, content_sha256: str = "a" * 64) -> None:
        self.root = root
        self.content_sha256 = content_sha256

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
            content_sha256=self.content_sha256,
            mime_type="image/jpeg",
            byte_size=10,
            width=1280,
            height=720,
        )


class FakeReviewer:
    def __init__(self, *, fail_once: bool = False, overall_risk: str = "risk") -> None:
        self.fail_once = fail_once
        self.overall_risk = overall_risk
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
                overall_risk=self.overall_risk,
                risk_tags=("未成年人",) if self.overall_risk != "safe" else (),
                summary="封面包含疑似未成年人风险元素" if self.overall_risk != "safe" else "本次封面未见明确风险",
                evidence="画面主体呈现学生制服与校园文字" if self.overall_risk != "safe" else "当前画面未见此前风险元素",
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


def test_partial_channel_scan_with_successful_item_marks_run_partial_failed(tmp_path: Path) -> None:
    store, scope, run, reviewer, _ = _create_executor_fixture(tmp_path)
    executor = CoverRunExecutor(
        store=store,
        collector=PartialCollector(),
        downloader=FakeDownloader(tmp_path / "partial-assets"),
        reviewer=reviewer,
        worker_name="cover-worker-partial",
    )

    result = executor.run_once()

    assert result is not None
    assert result["status"] == "partial_failed"
    assert result["completed_item_count"] == 1
    assert result["failed_item_count"] == 0
    assert "频道扫描不完整" in result["status_message"]


def test_partial_channel_scan_without_items_does_not_report_completed(tmp_path: Path) -> None:
    store, scope, run, _, _ = _create_executor_fixture(tmp_path)
    executor = CoverRunExecutor(
        store=store,
        collector=PartialCollector(include_video=False),
        downloader=FakeDownloader(tmp_path / "partial-empty-assets"),
        reviewer=FakeReviewer(),
        worker_name="cover-worker-partial-empty",
    )

    result = executor.run_once()

    assert result is not None
    assert result["status"] == "partial_failed"
    assert result["total_item_count"] == 0
    assert "频道扫描不完整" in result["status_message"]


def test_safe_redetection_on_changed_cover_creates_candidate_without_closing_case(tmp_path: Path) -> None:
    store, scope, _, _, executor = _create_executor_fixture(tmp_path)
    first_result = executor.run_once()
    assert first_result is not None and first_result["status"] == "completed"

    channel = store.list_channels(scope)[0]
    second_run = store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": True},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    second_executor = CoverRunExecutor(
        store=store,
        collector=FakeCollector(),
        downloader=FakeDownloader(tmp_path / "changed-assets", content_sha256="b" * 64),
        reviewer=FakeReviewer(overall_risk="safe"),
        worker_name="cover-worker-safe-redetection",
    )

    second_result = second_executor.run_once()

    assert second_result is not None
    assert second_result["run_id"] == second_run["run_id"]
    assert second_result["status"] == "completed"
    with store._connect() as conn:
        assets = conn.execute(
            "SELECT content_sha256 FROM cover_assets ORDER BY created_at, asset_id"
        ).fetchall()
        detections = conn.execute(
            "SELECT overall_risk FROM cover_detections ORDER BY created_at, detection_id"
        ).fetchall()
        risk_cases = conn.execute(
            "SELECT current_status, closed_at FROM cover_risk_cases"
        ).fetchall()
        events = conn.execute(
            "SELECT event_type FROM cover_case_events ORDER BY case_event_id"
        ).fetchall()
    assert {row[0] for row in assets} == {"a" * 64, "b" * 64}
    assert {row[0] for row in detections} == {"risk", "safe"}
    assert [tuple(row) for row in risk_cases] == [("needs_review", None)]
    assert [row[0] for row in events] == ["risk_detected", "rectification_candidate"]

    case_id = store.list_risk_cases(scope, status="needs_review")["items"][0]["case_id"]
    reviewed = store.review_risk_case(
        scope,
        case_id,
        action="confirm_rectified",
        reason="已核对新旧封面，确认风险元素已移除",
    )

    assert reviewed["current_status"] == "confirmed_rectified"
    assert reviewed["closed_at"] is not None
    with store._connect() as conn:
        review = conn.execute(
            "SELECT action, reviewed_by_user_id FROM cover_case_reviews WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        user_event = conn.execute(
            "SELECT event_type, actor_type, actor_user_id FROM cover_case_events "
            "WHERE case_id = ? ORDER BY case_event_id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    assert tuple(review) == ("confirm_rectified", scope.user_id)
    assert tuple(user_event) == ("confirmed_rectified", "user", scope.user_id)


def test_safe_redetection_on_same_cover_is_model_variance_not_rectification(tmp_path: Path) -> None:
    store, scope, _, _, executor = _create_executor_fixture(tmp_path)
    executor.run_once()
    channel = store.list_channels(scope)[0]
    store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": True},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    follow_up = CoverRunExecutor(
        store=store,
        collector=FakeCollector(),
        downloader=FakeDownloader(tmp_path / "same-assets", content_sha256="a" * 64),
        reviewer=FakeReviewer(overall_risk="safe"),
        worker_name="cover-worker-model-variance",
    )

    follow_up.run_once()

    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_assets").fetchone()[0] == 1
        case = conn.execute("SELECT current_status, closed_at FROM cover_risk_cases").fetchone()
        events = conn.execute(
            "SELECT event_type FROM cover_case_events ORDER BY case_event_id"
        ).fetchall()
    assert tuple(case) == ("needs_review", None)
    assert [row[0] for row in events] == ["risk_detected", "safe_redetection"]
    case_id = store.list_risk_cases(scope, status="needs_review")["items"][0]["case_id"]
    with pytest.raises(ValueError, match="整改候选"):
        store.review_risk_case(
            scope,
            case_id,
            action="confirm_rectified",
            reason="不能仅依据同一图片的模型波动确认整改",
        )


def test_repeated_risk_detection_reuses_existing_open_case(tmp_path: Path) -> None:
    store, scope, _, _, executor = _create_executor_fixture(tmp_path)
    executor.run_once()
    channel = store.list_channels(scope)[0]
    store.create_run(
        scope,
        trigger_type="manual",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": True},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    follow_up = CoverRunExecutor(
        store=store,
        collector=FakeCollector(),
        downloader=FakeDownloader(tmp_path / "repeat-risk-assets", content_sha256="b" * 64),
        reviewer=FakeReviewer(overall_risk="risk"),
        worker_name="cover-worker-repeat-risk",
    )

    follow_up.run_once()

    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_risk_cases").fetchone()[0] == 1
        events = conn.execute(
            "SELECT event_type FROM cover_case_events ORDER BY case_event_id"
        ).fetchall()
    assert [row[0] for row in events] == ["risk_detected", "risk_detected"]


def test_new_risk_after_rectification_candidate_invalidates_confirmation(tmp_path: Path) -> None:
    store, scope, _, _, executor = _create_executor_fixture(tmp_path)
    executor.run_once()
    channel = store.list_channels(scope)[0]
    for index, overall_risk in enumerate(("safe", "risk"), start=1):
        store.create_run(
            scope,
            trigger_type="manual",
            intensity="standard",
            channel_pks=[channel["channel_pk"]],
            params={"force_refresh": True},
            model_snapshot={"provider": "fake", "model": "fake"},
            prompt_version="cover-visible-evidence-v1",
        )
        CoverRunExecutor(
            store=store,
            collector=FakeCollector(),
            downloader=FakeDownloader(
                tmp_path / f"candidate-invalidated-{index}",
                content_sha256=("b" if index == 1 else "c") * 64,
            ),
            reviewer=FakeReviewer(overall_risk=overall_risk),
            worker_name=f"cover-worker-candidate-invalidated-{index}",
        ).run_once()

    case_id = store.list_risk_cases(scope, status="needs_review")["items"][0]["case_id"]
    with pytest.raises(ValueError, match="整改候选"):
        store.review_risk_case(
            scope,
            case_id,
            action="confirm_rectified",
            reason="候选之后再次检出风险，不能确认整改",
        )


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


def test_incremental_scan_on_existing_channel_only_processes_new_video(tmp_path: Path) -> None:
    store, scope, _, reviewer, executor = _create_executor_fixture(tmp_path)
    first_result = executor.run_once()
    assert first_result is not None and first_result["completed_item_count"] == 1

    channel = store.list_channels(scope)[0]
    second_run = store.create_run(
        scope,
        trigger_type="schedule",
        intensity="standard",
        channel_pks=[channel["channel_pk"]],
        params={"force_refresh": False},
        model_snapshot={"provider": "fake", "model": "fake"},
        prompt_version="cover-visible-evidence-v1",
    )
    second_executor = CoverRunExecutor(
        store=store,
        collector=FakeCollector("video-executor-002"),
        downloader=FakeDownloader(tmp_path / "second-assets"),
        reviewer=reviewer,
        worker_name="cover-worker-incremental",
    )

    second_result = second_executor.run_once()

    assert second_result is not None
    assert second_result["status"] == "completed"
    assert second_result["total_item_count"] == 1
    assert reviewer.calls == 2
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cover_videos").fetchone()[0] == 2
        task = conn.execute(
            "SELECT video.video_id, item.reason FROM cover_task_items AS item "
            "JOIN cover_videos AS video ON video.video_pk = item.video_pk "
            "WHERE item.run_id = ?",
            (second_run["run_id"],),
        ).fetchone()
    assert tuple(task) == ("video-executor-002", "new_video")
