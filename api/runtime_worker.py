from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Optional

from api.config import SETTINGS
from service.task_executor import run_next_queued_task


logger = logging.getLogger(__name__)


@dataclass
class AutoWorkerHandle:
    thread: threading.Thread
    stop_event: threading.Event


def _run_worker_loop(stop_event: threading.Event) -> None:
    poll_seconds = max(float(SETTINGS.auto_worker_poll_seconds), 0.5)
    worker_name = f"{SETTINGS.task_worker_name}-api-auto"

    while not stop_event.is_set():
        try:
            semantic_config = SETTINGS.build_semantic_config()
            task = run_next_queued_task(
                business_db_path=SETTINGS.business_db_path,
                retrieval_db_path=SETTINGS.db_path,
                semantic_config=semantic_config,
                export_root=SETTINGS.task_export_root,
                worker_name=worker_name,
            )
            if task is None:
                stop_event.wait(poll_seconds)
                continue
            logger.info("auto worker processed task=%s status=%s", task.get("task_id"), task.get("status"))
        except Exception:  # pragma: no cover - defensive background loop
            logger.exception("auto worker loop failed")
            stop_event.wait(poll_seconds)


def start_auto_worker() -> Optional[AutoWorkerHandle]:
    if not SETTINGS.auto_worker_enabled:
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_worker_loop,
        args=(stop_event,),
        name="novel-similarity-auto-worker",
        daemon=True,
    )
    thread.start()
    logger.info("auto worker started")
    return AutoWorkerHandle(thread=thread, stop_event=stop_event)


def stop_auto_worker(handle: Optional[AutoWorkerHandle]) -> None:
    if handle is None:
        return
    handle.stop_event.set()
    handle.thread.join(timeout=10)
    logger.info("auto worker stopped")
