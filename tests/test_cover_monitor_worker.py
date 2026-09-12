from __future__ import annotations

from datetime import datetime, timezone
import threading

from service.cover_monitor.worker import run_cover_worker_loop


class FakeStore:
    def __init__(self) -> None:
        self.stale_before_values: list[str] = []

    def recover_stale_runs(self, *, stale_before: str) -> dict[str, int]:
        self.stale_before_values.append(stale_before)
        return {"requeued": 1, "paused": 0, "cancelled": 0}


class FakeExecutor:
    def __init__(self, results: list[dict | None]) -> None:
        self.results = list(results)
        self.calls = 0

    def run_once(self) -> dict | None:
        self.calls += 1
        return self.results.pop(0)


def test_cover_worker_once_recovers_stale_runs_before_polling_queue() -> None:
    store = FakeStore()
    executor = FakeExecutor([None])

    processed = run_cover_worker_loop(
        store=store,
        executor=executor,
        stop_event=threading.Event(),
        once=True,
        stale_after_seconds=120,
        now_utc=lambda: datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )

    assert processed == 0
    assert executor.calls == 1
    assert store.stale_before_values == ["2026-09-12T11:58:00+00:00"]


def test_cover_worker_counts_processed_runs_and_stops_after_once() -> None:
    store = FakeStore()
    executor = FakeExecutor([{"run_id": "run-1", "status": "completed"}])

    processed = run_cover_worker_loop(
        store=store,
        executor=executor,
        stop_event=threading.Event(),
        once=True,
    )

    assert processed == 1
    assert executor.calls == 1
