from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import time
import threading
from pathlib import Path
from typing import Any

from service.drama_subtitle_hybrid_retrieval import search_drama_subtitle_hybrid_candidates
from service.drama_subtitle_semantic_retrieval import DramaSubtitleSemanticConfig
from service.drama_subtitle_translation import DramaSubtitleTranslationConfig, apply_translation_fallback
from service.drama_subtitle_task_store import (
    claim_next_drama_subtitle_task,
    finish_drama_subtitle_task,
    get_drama_subtitle_task,
    list_drama_subtitle_task_items,
    replace_drama_subtitle_task_items,
    save_drama_subtitle_task_item_outcome,
    settle_drama_subtitle_task_for_worker_shutdown,
)
from service.task_input_parser import parse_task_input_file


ROOT_DIR = Path(__file__).resolve().parent.parent


class _ShutdownAwareThreadPoolExecutor(ThreadPoolExecutor):
    def __init__(self, *args: Any, stop_event: threading.Event | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stop_event = stop_event

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        stopping = self._stop_event is not None and self._stop_event.is_set()
        self.shutdown(wait=not stopping, cancel_futures=stopping)
        return False


def _resolve_source_path(raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else (ROOT_DIR / path).resolve()


def _resolve_bool(raw_value: object, default: bool) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def execute_claimed_drama_subtitle_task(
    *,
    task_id: str,
    business_db_path: str | Path,
    subtitle_db_path: str | Path,
    semantic_config: DramaSubtitleSemanticConfig = DramaSubtitleSemanticConfig(),
    semantic_enabled_default: bool = True,
    translation_config: DramaSubtitleTranslationConfig = DramaSubtitleTranslationConfig(),
    stop_event: threading.Event | None = None,
    worker_lease_token: str | None = None,
) -> dict[str, Any]:
    task = get_drama_subtitle_task(business_db_path, task_id)
    if task is None:
        raise ValueError(f"drama subtitle task not found: {task_id}")
    if stop_event is not None and stop_event.is_set():
        settle_drama_subtitle_task_for_worker_shutdown(
            db_path=business_db_path,
            task_id=task_id,
            worker_lease_token=worker_lease_token,
        )
        return get_drama_subtitle_task(business_db_path, task_id) or task
    source_path = _resolve_source_path(str(task["source_file_path"]))
    try:
        items = list_drama_subtitle_task_items(business_db_path, task_id)
        if not items:
            if not source_path.exists():
                raise FileNotFoundError(f"input file not found: {source_path}")
            parsed = parse_task_input_file(source_path)
            if not parsed:
                raise ValueError("input file contains no executable subtitle text")
            replace_drama_subtitle_task_items(
                db_path=business_db_path,
                task_id=task_id,
                items=[item.__dict__ for item in parsed],
                worker_lease_token=worker_lease_token,
            )
            items = list_drama_subtitle_task_items(business_db_path, task_id)

        params = dict(task.get("params") or {})
        top_k = int(params.get("top_k") or 10)
        window_limit = int(params.get("window_limit") or 200)
        semantic_enabled = _resolve_bool(params.get("semantic_enabled"), semantic_enabled_default)
        semantic_window_limit = int(params.get("semantic_window_limit") or 100)
        translation_enabled = _resolve_bool(params.get("translation_fallback"), translation_config.enabled)
        completed = 0
        failed = 0
        for item in items:
            if stop_event is not None and stop_event.is_set():
                settle_drama_subtitle_task_for_worker_shutdown(
                    db_path=business_db_path,
                    task_id=task_id,
                    worker_lease_token=worker_lease_token,
                )
                return get_drama_subtitle_task(business_db_path, task_id) or task
            live_task = get_drama_subtitle_task(business_db_path, task_id)
            live_status = str((live_task or {}).get("status") or "")
            if live_status == "pause_requested":
                finish_drama_subtitle_task(
                    db_path=business_db_path,
                    task_id=task_id,
                    status="paused",
                    status_message=f"Task paused after {completed + failed}/{len(items)} subtitle inputs.",
                    worker_lease_token=worker_lease_token,
                )
                return get_drama_subtitle_task(business_db_path, task_id) or task
            if live_status == "cancel_requested":
                finish_drama_subtitle_task(
                    db_path=business_db_path,
                    task_id=task_id,
                    status="cancelled",
                    status_message=f"Task cancelled after {completed + failed}/{len(items)} subtitle inputs.",
                    worker_lease_token=worker_lease_token,
                )
                return get_drama_subtitle_task(business_db_path, task_id) or task
            if str(item["status"]) == "completed":
                completed += 1
                continue
            if str(item["status"]) == "failed":
                failed += 1
                continue
            started = time.perf_counter()
            try:
                active_translation_config = type(translation_config)(
                    **{**translation_config.__dict__, "enabled": translation_enabled}
                )
                with _ShutdownAwareThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix=f"subtitle-{task_id[:8]}",
                    stop_event=stop_event,
                ) as item_executor:
                    future = item_executor.submit(
                        _run_subtitle_item_pipeline,
                        query_text=str(item["query_text"]),
                        subtitle_db_path=str(subtitle_db_path),
                        business_db_path=str(business_db_path),
                        owner_user_id=int(task.get("owner_user_id") or 1),
                        top_k=top_k,
                        window_limit=window_limit,
                        semantic_enabled=semantic_enabled,
                        semantic_config=semantic_config,
                        semantic_window_limit=semantic_window_limit,
                        translation_config=active_translation_config,
                    )
                    while True:
                        done, _ = wait(
                            {future},
                            timeout=0.25 if stop_event is not None else None,
                            return_when=FIRST_COMPLETED,
                        )
                        if done:
                            payload = future.result()
                            break
                        if stop_event is not None and stop_event.is_set():
                            settle_drama_subtitle_task_for_worker_shutdown(
                                db_path=business_db_path,
                                task_id=task_id,
                                worker_lease_token=worker_lease_token,
                            )
                            return get_drama_subtitle_task(business_db_path, task_id) or task
                save_drama_subtitle_task_item_outcome(
                    db_path=business_db_path,
                    task_id=task_id,
                    item_order=int(item["item_order"]),
                    status="completed",
                    duration_seconds=round(time.perf_counter() - started, 4),
                    query_language_code=str(payload.get("query_language_code") or "unknown"),
                    query_language_confidence=float(payload.get("query_language_confidence") or 0.0),
                    result_payload=payload,
                    worker_lease_token=worker_lease_token,
                )
                completed += 1
            except Exception as exc:
                save_drama_subtitle_task_item_outcome(
                    db_path=business_db_path,
                    task_id=task_id,
                    item_order=int(item["item_order"]),
                    status="failed",
                    duration_seconds=round(time.perf_counter() - started, 4),
                    query_language_code="unknown",
                    query_language_confidence=0.0,
                    error_message=str(exc),
                    worker_lease_token=worker_lease_token,
                )
                failed += 1
        if stop_event is not None and stop_event.is_set():
            settle_drama_subtitle_task_for_worker_shutdown(
                db_path=business_db_path,
                task_id=task_id,
                worker_lease_token=worker_lease_token,
            )
            return get_drama_subtitle_task(business_db_path, task_id) or task
        status = "completed" if failed == 0 else "partial_failed"
        finish_drama_subtitle_task(
            db_path=business_db_path,
            task_id=task_id,
            status=status,
            status_message=f"Processed {completed + failed}/{len(items)} subtitle inputs.",
            worker_lease_token=worker_lease_token,
        )
    except Exception as exc:
        finish_drama_subtitle_task(
            db_path=business_db_path,
            task_id=task_id,
            status="failed",
            status_message="Subtitle task failed.",
            error_message=str(exc),
            worker_lease_token=worker_lease_token,
        )
    return get_drama_subtitle_task(business_db_path, task_id) or task


def _run_subtitle_item_pipeline(
    *,
    query_text: str,
    subtitle_db_path: str,
    business_db_path: str,
    owner_user_id: int,
    top_k: int,
    window_limit: int,
    semantic_enabled: bool,
    semantic_config: DramaSubtitleSemanticConfig,
    semantic_window_limit: int,
    translation_config: DramaSubtitleTranslationConfig,
) -> dict[str, Any]:
    payload = search_drama_subtitle_hybrid_candidates(
        db_path=subtitle_db_path,
        query_text=query_text,
        candidate_limit=top_k,
        window_limit=window_limit,
        include_window_text=True,
        semantic_enabled=semantic_enabled,
        semantic_config=semantic_config,
        semantic_window_limit=semantic_window_limit,
    )
    return apply_translation_fallback(
        native_payload=payload,
        query_text=query_text,
        search_function=search_drama_subtitle_hybrid_candidates,
        search_kwargs={
            "db_path": subtitle_db_path,
            "candidate_limit": top_k,
            "window_limit": window_limit,
            "include_window_text": True,
            "semantic_enabled": semantic_enabled,
            "semantic_config": semantic_config,
            "semantic_window_limit": semantic_window_limit,
        },
        config=translation_config,
        business_db_path=business_db_path,
        owner_user_id=owner_user_id,
    )


def run_next_queued_drama_subtitle_task(
    *,
    business_db_path: str | Path,
    subtitle_db_path: str | Path,
    worker_name: str,
    semantic_config: DramaSubtitleSemanticConfig = DramaSubtitleSemanticConfig(),
    semantic_enabled_default: bool = True,
    translation_config: DramaSubtitleTranslationConfig = DramaSubtitleTranslationConfig(),
    stop_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    task = claim_next_drama_subtitle_task(business_db_path, worker_name=worker_name)
    if task is None:
        return None
    return execute_claimed_drama_subtitle_task(
        task_id=str(task["task_id"]),
        business_db_path=business_db_path,
        subtitle_db_path=subtitle_db_path,
        semantic_config=semantic_config,
        semantic_enabled_default=semantic_enabled_default,
        translation_config=translation_config,
        stop_event=stop_event,
        worker_lease_token=str(task.get("_worker_lease_token") or "") or None,
    )
