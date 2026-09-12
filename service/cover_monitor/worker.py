from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import threading
import time
from typing import Any, Callable


logger = logging.getLogger(__name__)


def run_cover_worker_loop(
    *,
    store: Any,
    executor: Any,
    stop_event: threading.Event,
    once: bool = False,
    poll_seconds: float = 3,
    stale_after_seconds: float = 120,
    recovery_sweep_seconds: float = 15,
    now_utc: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    poll_seconds = max(float(poll_seconds), 0.5)
    stale_after_seconds = max(float(stale_after_seconds), 1)
    recovery_sweep_seconds = max(float(recovery_sweep_seconds), 0.5)
    now_utc = now_utc or (lambda: datetime.now(timezone.utc))
    next_recovery_at = 0.0
    processed = 0

    while not stop_event.is_set():
        current_tick = monotonic()
        if current_tick >= next_recovery_at:
            stale_before = now_utc() - timedelta(seconds=stale_after_seconds)
            recovered = store.recover_stale_runs(stale_before=stale_before.isoformat(timespec="seconds"))
            if any(int(value or 0) > 0 for value in recovered.values()):
                logger.warning("cover worker recovered stale runs: %s", recovered)
            next_recovery_at = current_tick + recovery_sweep_seconds
        try:
            result = executor.run_once()
        except Exception:
            if once:
                raise
            logger.exception("cover worker run failed")
            stop_event.wait(poll_seconds)
            continue
        if result is not None:
            processed += 1
            logger.info(
                "cover worker processed run=%s status=%s",
                result.get("run_id"),
                result.get("status"),
            )
        if once:
            break
        if result is None:
            stop_event.wait(poll_seconds)
    return processed
