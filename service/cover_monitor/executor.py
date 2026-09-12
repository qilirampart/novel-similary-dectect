from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator

from service.cover_monitor.collector import CoverCollectionCancelled
from service.cover_monitor.downloader import CoverDownloadError
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


class _Heartbeat(AbstractContextManager["_Heartbeat"]):
    def __init__(self, callback: Callable[[], bool], interval_seconds: float) -> None:
        self.callback = callback
        self.interval_seconds = max(float(interval_seconds), 0.05)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "_Heartbeat":
        self.thread = threading.Thread(target=self._run, name="cover-lease-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            if not self.callback():
                return


class CoverRunExecutor:
    RETRYABLE_ERRORS = {"timeout", "network", "rate_limit", "provider_http"}

    def __init__(
        self,
        *,
        store: CoverMonitorStore,
        collector: Any,
        downloader: Any,
        reviewer: Any,
        worker_name: str,
        max_attempts: int = 3,
        retry_delay_seconds: float = 5,
        heartbeat_interval_seconds: float = 5,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.collector = collector
        self.downloader = downloader
        self.reviewer = reviewer
        self.worker_name = str(worker_name or "").strip()
        if not self.worker_name:
            raise ValueError("worker_name is required")
        self.max_attempts = max(int(max_attempts), 1)
        self.retry_delay_seconds = max(float(retry_delay_seconds), 0)
        self.heartbeat_interval_seconds = max(float(heartbeat_interval_seconds), 0.05)
        self.sleep = sleep
        self.should_stop = should_stop or (lambda: False)

    def run_once(self) -> dict[str, Any] | None:
        run = self.store.claim_next_run(worker_name=self.worker_name)
        if run is None:
            return None
        run_id = str(run["run_id"])
        run_lease = str(run["worker_lease_token"])
        scope = CoverAccessScope(workspace_key=str(run["workspace_key"]), user_id=int(run["created_by_user_id"]))
        params = json.loads(str(run.get("params_json") or "{}"))

        if self._settle_interruption(scope, run_id, run_lease):
            return self.store.get_run(scope, run_id)
        if not self._scan_channels(scope, run, run_lease, params):
            return self.store.get_run(scope, run_id)
        if self._settle_interruption(scope, run_id, run_lease):
            return self.store.get_run(scope, run_id)
        self._process_items(scope, run, run_lease)
        current = self.store.get_run(scope, run_id)
        if current is not None and current["status"] == "running" and int(current["total_item_count"]) == 0:
            self.store.finish_run_without_items(run_id, run_lease)
        return self.store.get_run(scope, run_id)

    def _scan_channels(
        self,
        scope: CoverAccessScope,
        run: dict[str, Any],
        run_lease: str,
        params: dict[str, Any],
    ) -> bool:
        run_id = str(run["run_id"])
        force_refresh = bool(params.get("force_refresh", False))
        for channel in self.store.list_run_channels(run_id, run_lease):
            if str(channel.get("scan_status")) == "completed":
                continue
            if self._settle_interruption(scope, run_id, run_lease):
                return False
            try:
                with self._run_heartbeat(run_id, run_lease):
                    collection = self.collector.collect(
                        str(channel["source_url"]),
                        max_items_per_scope=max(int(params.get("max_items_per_scope") or 0), 0),
                        include_shorts=bool(params.get("include_shorts", True)),
                        should_cancel=lambda: self.should_stop() or self._control_requested(scope, run_id),
                    )
                task_items: list[dict[str, Any]] = []
                scanned_videos: list[tuple[int, bool]] = []
                for video in collection.videos:
                    existing = self.store.get_video_by_identity(
                        scope,
                        platform=str(channel["platform"]),
                        video_id=video.video_id,
                    )
                    saved = self.store.upsert_video(
                        scope,
                        channel_pk=int(channel["channel_pk"]),
                        platform=str(channel["platform"]),
                        video_id=video.video_id,
                        title=video.title,
                        video_url=video.video_url,
                        thumbnail_url=video.thumbnail_url,
                        upload_date=video.upload_date,
                    )
                    scanned_videos.append((int(saved["video_pk"]), existing is None))
                existing_video_pks = [video_pk for video_pk, is_new in scanned_videos if not is_new]
                scan_reasons = self.store.get_video_scan_reasons(scope, existing_video_pks)
                for video_pk, is_new in scanned_videos:
                    reason = "manual" if force_refresh else (
                        "new_video" if is_new else scan_reasons.get(video_pk)
                    )
                    if reason is not None:
                        task_items.append({
                            "video_pk": video_pk,
                            "reason": reason,
                        })
                self.store.enqueue_task_items(run_id, run_lease, task_items)
                self.store.finish_run_channel(
                    run_id,
                    run_lease,
                    int(channel["run_channel_id"]),
                    completeness=collection.completeness,
                    discovered_count=len(collection.videos),
                    checkpoint={
                        "scopes_completed": list(collection.scopes_completed),
                        "scope_item_counts": collection.scope_item_counts,
                        "scope_errors": collection.scope_errors,
                    },
                    error_message="; ".join(collection.scope_errors.values()),
                )
            except CoverCollectionCancelled:
                self._settle_interruption(scope, run_id, run_lease)
                return False
            except Exception as exc:
                self.store.finish_run_channel(
                    run_id,
                    run_lease,
                    int(channel["run_channel_id"]),
                    completeness="failed",
                    discovered_count=0,
                    checkpoint={},
                    error_message=f"{type(exc).__name__}: {str(exc)[:800]}",
                )
            self.store.heartbeat_run(run_id, run_lease)
        return True

    def _process_items(self, scope: CoverAccessScope, run: dict[str, Any], run_lease: str) -> None:
        run_id = str(run["run_id"])
        while True:
            if self._settle_interruption(scope, run_id, run_lease):
                return
            item = self.store.claim_next_task_item(run_id, run_lease)
            if item is None:
                state = self.store.get_run_item_state(run_id, run_lease)
                if state is None or int(state["unfinished_count"] or 0) == 0:
                    return
                next_retry_at = str(state.get("next_retry_at") or "")
                if not next_retry_at or not self._wait_for_retry(scope, run_id, run_lease, next_retry_at):
                    return
                continue
            self._process_item(scope, run, run_lease, item)

    def _process_item(
        self,
        scope: CoverAccessScope,
        run: dict[str, Any],
        run_lease: str,
        item: dict[str, Any],
    ) -> bool:
        task_item_id = int(item["task_item_id"])
        item_lease = str(item["worker_lease_token"])
        context = self.store.get_task_item_context(task_item_id, item_lease)
        if context is None:
            return False
        try:
            if str(context["stage"]) == "persist":
                self.store.finish_task_item(task_item_id, item_lease, succeeded=True)
                return False
            asset = self.store.get_latest_asset(scope, int(context["video_pk"]))
            if str(context["stage"]) == "download":
                with self._item_heartbeat(str(run["run_id"]), run_lease, task_item_id, item_lease):
                    downloaded = self.downloader.download(
                        str(context["video_id"]),
                        str(context["thumbnail_url"]),
                    )
                asset = self.store.save_asset(scope, video_pk=int(context["video_pk"]), asset=asdict(downloaded))
                if not self.store.advance_task_item(
                    task_item_id,
                    item_lease,
                    expected_stage="download",
                    next_stage="review",
                ):
                    return False
                context["stage"] = "review"
            if asset is None:
                raise RuntimeError("cover asset is missing before review")
            attempt = self.store.start_task_attempt(
                task_item_id,
                item_lease,
                stage="review",
                provider=str(run.get("model_snapshot_json") or ""),
            )
            if attempt is None:
                return False
            with self._item_heartbeat(str(run["run_id"]), run_lease, task_item_id, item_lease):
                with self._materialized_asset(asset) as image_path:
                    outcome = self.reviewer.review(
                        image_path=image_path,
                        video_title=str(context["title"]),
                        intensity=str(run["intensity"]),
                    )
            self.store.finish_task_attempt(
                int(attempt["attempt_id"]),
                succeeded=outcome.status == "succeeded" and outcome.result is not None,
                duration_seconds=float(outcome.duration_seconds),
                error_type=str(outcome.error_type or ""),
                error_message=str(outcome.error_message or ""),
                usage={"provider_status": outcome.provider_status},
            )
            if outcome.status != "succeeded" or outcome.result is None:
                return self._handle_item_failure(
                    item,
                    error_type=str(outcome.error_type or "unexpected"),
                    error_message=str(outcome.error_message or "cover review failed"),
                )
            detection = outcome.result.to_dict()
            detection["input_snapshot"] = {
                "video_id": str(context["video_id"]),
                "title": str(context["title"]),
                "thumbnail_url": str(context["thumbnail_url"]),
                "asset_sha256": str(asset["content_sha256"]),
            }
            self.store.save_detection_and_case(
                scope,
                task_item_id=task_item_id,
                worker_lease_token=item_lease,
                asset_id=str(asset["asset_id"]),
                detection=detection,
            )
            if not self.store.advance_task_item(
                task_item_id,
                item_lease,
                expected_stage="review",
                next_stage="persist",
            ):
                return False
            self.store.finish_task_item(task_item_id, item_lease, succeeded=True)
            return False
        except Exception as exc:
            return self._handle_item_failure(
                item,
                error_type=self._classify_item_exception(exc),
                error_message=f"{type(exc).__name__}: {str(exc)[:800]}",
            )

    def _handle_item_failure(self, item: dict[str, Any], *, error_type: str, error_message: str) -> bool:
        attempts = int(item.get("attempts") or 0)
        task_item_id = int(item["task_item_id"])
        item_lease = str(item["worker_lease_token"])
        if error_type in self.RETRYABLE_ERRORS and attempts < self.max_attempts:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=self.retry_delay_seconds)
            return self.store.retry_task_item(
                task_item_id,
                item_lease,
                next_retry_at=retry_at.isoformat(timespec="milliseconds"),
                error_type=error_type,
                error_message=error_message,
            )
        self.store.finish_task_item(
            task_item_id,
            item_lease,
            succeeded=False,
            error_type=error_type,
            error_message=error_message,
        )
        return False

    def _asset_path(self, asset: dict[str, Any]) -> Path:
        root = getattr(self.downloader, "asset_root", None) or getattr(self.downloader, "root", None)
        if root is None:
            raise RuntimeError("downloader does not expose its asset root")
        return Path(root) / str(asset["storage_key"])

    @contextmanager
    def _materialized_asset(self, asset: dict[str, Any]) -> Iterator[Path]:
        storage = getattr(self.downloader, "storage", None)
        if storage is not None:
            with storage.materialize(str(asset["storage_key"])) as path:
                yield Path(path)
            return
        yield self._asset_path(asset)

    def _wait_for_retry(
        self,
        scope: CoverAccessScope,
        run_id: str,
        run_lease: str,
        next_retry_at: str,
    ) -> bool:
        try:
            retry_at = datetime.fromisoformat(next_retry_at.replace("Z", "+00:00"))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        while True:
            if self._settle_interruption(scope, run_id, run_lease):
                return False
            remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return True
            self.store.heartbeat_run(run_id, run_lease)
            self.sleep(min(remaining, 1.0))

    @staticmethod
    def _classify_item_exception(exc: Exception) -> str:
        if isinstance(exc, CoverDownloadError):
            return "network"
        if isinstance(exc, OSError):
            return "local_io"
        return "unexpected"

    def _run_heartbeat(self, run_id: str, run_lease: str) -> _Heartbeat:
        return _Heartbeat(
            lambda: self.store.heartbeat_run(run_id, run_lease),
            self.heartbeat_interval_seconds,
        )

    def _item_heartbeat(
        self,
        run_id: str,
        run_lease: str,
        task_item_id: int,
        item_lease: str,
    ) -> _Heartbeat:
        def heartbeat() -> bool:
            return self.store.heartbeat_run(run_id, run_lease) and self.store.heartbeat_task_item(
                task_item_id, item_lease
            )

        return _Heartbeat(heartbeat, self.heartbeat_interval_seconds)

    def _control_requested(self, scope: CoverAccessScope, run_id: str) -> bool:
        run = self.store.get_run(scope, run_id)
        return run is None or str(run["status"]) in {"pause_requested", "cancel_requested"}

    def _settle_control(self, scope: CoverAccessScope, run_id: str, run_lease: str) -> bool:
        run = self.store.get_run(scope, run_id)
        if run is None or str(run["status"]) not in {"pause_requested", "cancel_requested"}:
            return False
        self.store.settle_requested_control(run_id, run_lease)
        return True

    def _settle_interruption(self, scope: CoverAccessScope, run_id: str, run_lease: str) -> bool:
        if self.should_stop():
            self.store.release_run_for_worker_shutdown(run_id, run_lease)
            return True
        return self._settle_control(scope, run_id, run_lease)
