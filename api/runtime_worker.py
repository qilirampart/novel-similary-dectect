from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Optional

from api.config import SETTINGS
from service.business_store import claim_next_queued_task_globally, recover_interrupted_tasks
from service.task_executor import execute_claimed_task
from service.drama_subtitle_task_executor import execute_claimed_drama_subtitle_task
from service.drama_subtitle_task_store import recover_interrupted_drama_subtitle_tasks


logger = logging.getLogger(__name__)


@dataclass
class AutoWorkerHandle:
    threads: list[threading.Thread]
    stop_event: threading.Event
    maintenance_thread: threading.Thread | None = None


def _run_worker_loop(stop_event: threading.Event, worker_index: int) -> None:
    poll_seconds = max(float(SETTINGS.auto_worker_poll_seconds), 0.5)
    worker_name = f"{SETTINGS.task_worker_name}-api-auto-{worker_index}"

    while not stop_event.is_set():
        try:
            semantic_config = SETTINGS.build_semantic_config()
            claimed = claim_next_queued_task_globally(
                SETTINGS.business_db_path,
                worker_name=worker_name,
                stale_after_seconds=SETTINGS.task_recovery_stale_seconds,
            )
            if claimed is None:
                stop_event.wait(poll_seconds)
                continue
            if claimed["task_kind"] == "compare":
                task = execute_claimed_task(
                    task_id=claimed["task_id"],
                    worker_lease_token=claimed.get("worker_lease_token"),
                    business_db_path=SETTINGS.business_db_path,
                    retrieval_db_path=SETTINGS.db_path,
                    semantic_config=semantic_config,
                    export_root=SETTINGS.task_export_root,
                    item_parallelism=SETTINGS.task_item_parallelism,
                    stop_event=stop_event,
                )
            else:
                task = execute_claimed_drama_subtitle_task(
                    task_id=claimed["task_id"],
                    worker_lease_token=claimed.get("worker_lease_token"),
                    business_db_path=SETTINGS.business_db_path,
                    subtitle_db_path=SETTINGS.drama_subtitle_db_path,
                    semantic_config=SETTINGS.build_drama_subtitle_semantic_config(),
                    semantic_enabled_default=SETTINGS.drama_subtitle_semantic_enabled,
                    translation_config=SETTINGS.build_drama_subtitle_translation_config(),
                    stop_event=stop_event,
                )
            logger.info("auto worker processed task=%s status=%s", task.get("task_id"), task.get("status"))
        except Exception:  # pragma: no cover - defensive background loop
            logger.exception("auto worker loop failed")
            stop_event.wait(poll_seconds)


def _run_recovery_loop(stop_event: threading.Event) -> None:
    sweep_seconds = max(float(SETTINGS.task_recovery_sweep_seconds), 0.5)

    while not stop_event.is_set():
        try:
            summary = recover_interrupted_tasks(
                SETTINGS.business_db_path,
                stale_after_seconds=SETTINGS.task_recovery_stale_seconds,
            )
            subtitle_summary = recover_interrupted_drama_subtitle_tasks(
                SETTINGS.business_db_path,
                stale_after_seconds=SETTINGS.task_recovery_stale_seconds,
            )
            if any(int(summary.get(key, 0)) > 0 for key in summary) or any(
                int(subtitle_summary.get(key, 0)) > 0 for key in subtitle_summary
            ):
                logger.warning("auto worker recovery sweep repaired tasks: compare=%s subtitle=%s", summary, subtitle_summary)
        except Exception:  # pragma: no cover - defensive background loop
            logger.exception("auto worker recovery sweep failed")
        stop_event.wait(sweep_seconds)


def start_auto_worker() -> Optional[AutoWorkerHandle]:
    if not SETTINGS.auto_worker_enabled:
        return None
    worker_count = max(int(SETTINGS.auto_worker_count or 1), 1)
    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    maintenance_thread = threading.Thread(
        target=_run_recovery_loop,
        args=(stop_event,),
        name="novel-similarity-recovery-sweeper",
        daemon=True,
    )
    maintenance_thread.start()
    for worker_index in range(1, worker_count + 1):
        thread = threading.Thread(
            target=_run_worker_loop,
            args=(stop_event, worker_index),
            name=f"novel-similarity-auto-worker-{worker_index}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    logger.info("auto worker started count=%s", worker_count)
    return AutoWorkerHandle(
        threads=threads,
        stop_event=stop_event,
        maintenance_thread=maintenance_thread,
    )


def stop_auto_worker(handle: Optional[AutoWorkerHandle]) -> None:
    if handle is None:
        return
    handle.stop_event.set()
    shutdown_timeout = max(float(SETTINGS.worker_shutdown_timeout_seconds), 0.1)
    deadline = time.monotonic() + shutdown_timeout
    all_threads = list(handle.threads)
    if handle.maintenance_thread is not None:
        all_threads.append(handle.maintenance_thread)
    for thread in all_threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    alive_threads = [thread.name for thread in all_threads if thread.is_alive()]
    if alive_threads:
        logger.warning(
            "auto worker shutdown deadline exceeded timeout=%ss alive=%s",
            shutdown_timeout,
            alive_threads,
        )
    else:
        logger.info("auto worker stopped count=%s", len(handle.threads))
