from __future__ import annotations

import faulthandler
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
import logging
import os
from pathlib import Path
import signal
import shutil
import time
from typing import Optional
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

try:
    from pysqlite3 import dbapi2 as sqlite3  # type: ignore
except Exception:
    import sqlite3

from api.config import SETTINGS
from api.cover_routes import build_cover_monitor_router
from api.runtime_worker import start_auto_worker, stop_auto_worker
from api.schemas import (
    BasicTaskResponse,
    BulkActionResponse,
    CompareReviewRequest,
    CompareSingleRequest,
    CompareSingleResponse,
    CreateUserRequest,
    DramaSubtitleCompareRequest,
    DramaSubtitleCompareResponse,
    DramaSubtitleVideoCompareRequest,
    DramaSubtitleVideoCompareResponse,
    DramaSubtitleTaskCreateResponse,
    DramaSubtitleTaskDetailResponse,
    DramaSubtitleTaskListResponse,
    HealthResponse,
    LoginRequest,
    ResultListResponse,
    ReadinessResponse,
    SystemStatusResponse,
    TaskCreateResponse,
    TaskDetailResponse,
    TaskListResponse,
    TaskResultResponse,
    UpdateUserRequest,
    UserListResponse,
    UserProfileResponse,
)
from service.business_store import (
    authenticate_user,
    cancel_compare_task,
    clear_pending_review_results,
    connect_business_db,
    create_user,
    create_user_session,
    create_compare_task,
    delete_compare_task,
    get_compare_result,
    get_compare_task,
    get_compare_task_item_stats,
    get_business_db_lock_metrics,
    get_task_queue_metadata,
    get_session_user,
    init_business_db,
    list_compare_results,
    list_compare_task_items,
    list_compare_tasks,
    list_users,
    pause_compare_task,
    revoke_user_session,
    resume_compare_task,
    retry_compare_task,
    summarize_compare_results,
    update_user,
    upsert_compare_task_review,
)
from service.cover_monitor.store import init_cover_db
from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline
from service.drama_subtitle_evidence_context import (
    DramaSubtitleEvidenceContextError,
    get_drama_subtitle_evidence_context,
)
from service.drama_subtitle_hybrid_retrieval import search_drama_subtitle_hybrid_candidates
from service.drama_subtitle_retrieval import DramaSubtitleRetrievalError
from service.drama_subtitle_video_compare import compare_drama_subtitle_video
from service.drama_subtitle_translation import apply_translation_fallback
from service.drama_subtitle_export import build_drama_subtitle_review_export_xlsx
from service.drama_subtitle_task_store import (
    create_drama_subtitle_task,
    get_drama_subtitle_task,
    list_drama_subtitle_task_items,
    list_drama_subtitle_tasks,
    upsert_drama_subtitle_task_review,
    update_drama_subtitle_task_control,
)
from service.review_export import build_review_export_xlsx
from service.task_input_parser import parse_task_input_file

ROOT_DIR = Path(__file__).resolve().parent.parent
SEMANTIC_DISABLED_BACKENDS = {"", "0", "false", "off", "none", "disabled"}
SESSION_COOKIE_NAME = "novel_similarity_session"
SESSION_TTL_HOURS = 12
logger = logging.getLogger(__name__)
_FAULT_DIAGNOSTICS_SIGNAL_NUM: int | None = None
_FAULT_DIAGNOSTICS_ARMED = False


def _resolve_fault_diagnostics_signal() -> tuple[int | None, str]:
    signal_name = str(SETTINGS.fault_diagnostics_signal or "").strip().upper()
    if not SETTINGS.fault_diagnostics_enabled or not signal_name:
        return None, signal_name
    signal_value = getattr(signal, signal_name, None)
    if signal_value is None:
        return None, signal_name
    return int(signal_value), signal_name


def _configure_fault_diagnostics() -> None:
    global _FAULT_DIAGNOSTICS_SIGNAL_NUM, _FAULT_DIAGNOSTICS_ARMED
    if _FAULT_DIAGNOSTICS_ARMED:
        return
    if not hasattr(faulthandler, "register") or not hasattr(faulthandler, "unregister"):
        logger.info("fault diagnostics signal hooks are unavailable on this platform")
        return
    signal_num, signal_name = _resolve_fault_diagnostics_signal()
    if signal_num is None:
        if SETTINGS.fault_diagnostics_enabled:
            logger.warning(
                "fault diagnostics signal is unavailable: %s",
                signal_name or "<empty>",
            )
        return
    try:
        faulthandler.enable(all_threads=True)
        try:
            faulthandler.unregister(signal_num)
        except RuntimeError:
            pass
        faulthandler.register(signal_num, all_threads=True, chain=False)
        _FAULT_DIAGNOSTICS_SIGNAL_NUM = signal_num
        _FAULT_DIAGNOSTICS_ARMED = True
        logger.info(
            "fault diagnostics armed signal=%s pid=%s",
            signal_name,
            os.getpid(),
        )
    except Exception:  # pragma: no cover - defensive startup instrumentation
        logger.exception("failed to configure fault diagnostics")


def _teardown_fault_diagnostics() -> None:
    global _FAULT_DIAGNOSTICS_SIGNAL_NUM, _FAULT_DIAGNOSTICS_ARMED
    if not _FAULT_DIAGNOSTICS_ARMED or _FAULT_DIAGNOSTICS_SIGNAL_NUM is None:
        return
    try:
        faulthandler.unregister(_FAULT_DIAGNOSTICS_SIGNAL_NUM)
    except RuntimeError:
        pass
    _FAULT_DIAGNOSTICS_SIGNAL_NUM = None
    _FAULT_DIAGNOSTICS_ARMED = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    SETTINGS.ensure_runtime_dirs()
    init_business_db(SETTINGS.business_db_path)
    init_cover_db(SETTINGS.cover_monitor_db_path)
    _configure_fault_diagnostics()
    app.state.auto_worker_handle = start_auto_worker()
    try:
        yield
    finally:
        stop_auto_worker(getattr(app.state, "auto_worker_handle", None))
        app.state.auto_worker_handle = None
        _teardown_fault_diagnostics()


app = FastAPI(
    title="Novel Similarity Compare API",
    version="0.1.0",
    description="Novel similarity compare service V1 API",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.cors_allowed_origins_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.router.lifespan_context = lifespan


def _compute_file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_upload_suffix(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix not in {".txt", ".csv", ".tsv", ".xlsx"}:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")
    return suffix


async def _save_upload_file(task_id: str, upload: UploadFile) -> tuple[Path, int, str]:
    file_name = Path(upload.filename or "batch_input.txt").name
    suffix = _validate_upload_suffix(file_name)
    task_dir = Path(SETTINGS.task_upload_root) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    target_path = (task_dir / f"source{suffix}").resolve()
    with target_path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    file_size = target_path.stat().st_size
    file_sha256 = _compute_file_sha256(target_path)
    return target_path, file_size, file_sha256


def _resolve_runtime_path(path_text: Optional[str]) -> Optional[Path]:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def _scalar_query(db_path: str, sql: str) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


def _load_recent_task_rows(limit: int = 5) -> list[sqlite3.Row]:
    conn = connect_business_db(SETTINGS.business_db_path)
    try:
        return conn.execute(
            """
            SELECT task_id,
                   task_type,
                   status,
                   detection_mode,
                   created_at,
                   started_at,
                   finished_at,
                   updated_at,
                   status_message,
                   error_message
              FROM compare_tasks
             WHERE COALESCE(is_deleted, 0) = 0
             ORDER BY updated_at DESC, task_id DESC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()


def _format_duration_seconds(started_at: Optional[str], finished_at: Optional[str]) -> str:
    if not started_at or not finished_at:
        return "running"
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except ValueError:
        return "unknown"
    seconds = max(int((end - start).total_seconds()), 0)
    minutes, rem = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {rem}s"
    return f"{rem}s"


def _build_system_status_payload() -> dict[str, object]:
    retrieval_db_exists = Path(SETTINGS.db_path).exists()
    business_db_exists = Path(SETTINGS.business_db_path).exists()
    semantic_backend_name = (SETTINGS.semantic_backend or "").strip()
    semantic_backend_disabled = semantic_backend_name.lower() in SEMANTIC_DISABLED_BACKENDS

    chapter_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM chapters")
    content_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM chapter_contents")
    evidence_window_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM evidence_windows")
    semantic_chunk_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM semantic_chunks")

    total_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE COALESCE(is_deleted, 0) = 0",
    )
    running_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE COALESCE(is_deleted, 0) = 0 AND status = 'running'",
    )
    queued_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE COALESCE(is_deleted, 0) = 0 AND status = 'queued'",
    )
    failure_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE COALESCE(is_deleted, 0) = 0 AND status IN ('failed', 'partial_failed')",
    )
    paused_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE COALESCE(is_deleted, 0) = 0 AND status IN ('paused', 'pause_requested')",
    )
    result_count = _scalar_query(
        SETTINGS.business_db_path,
        """
        SELECT COUNT(*)
          FROM compare_task_items i
          JOIN compare_tasks t
            ON t.task_id = i.task_id
         WHERE COALESCE(t.is_deleted, 0) = 0
        """,
    )
    review_count = _scalar_query(
        SETTINGS.business_db_path,
        """
        SELECT COUNT(*)
          FROM compare_task_reviews r
          JOIN compare_task_items i
            ON i.result_id = r.result_id
          JOIN compare_tasks t
            ON t.task_id = i.task_id
         WHERE COALESCE(t.is_deleted, 0) = 0
        """,
    )
    fallback_count = _scalar_query(
        SETTINGS.business_db_path,
        """
        SELECT COUNT(*)
          FROM compare_task_items i
          JOIN compare_tasks t
            ON t.task_id = i.task_id
         WHERE COALESCE(t.is_deleted, 0) = 0
           AND i.semantic_status IN ('fallback_lexical_only', 'fallback_semantic_timeout')
        """,
    )
    db_lock_metrics = get_business_db_lock_metrics()
    db_lock_event_count = sum(
        int(metric.get("lock_events", 0)) for metric in db_lock_metrics.values()
    )
    db_lock_skipped_count = sum(
        int(metric.get("skipped", 0)) for metric in db_lock_metrics.values()
    )
    db_lock_wait_max = max(
        (float(metric.get("wait_seconds_max", 0.0)) for metric in db_lock_metrics.values()),
        default=0.0,
    )

    status_value = "healthy" if retrieval_db_exists and business_db_exists else "degraded"
    status_tone = "green" if status_value == "healthy" else "orange"
    recent_tasks = _load_recent_task_rows(limit=5)

    cards = [
        {
            "label": "Platform Health",
            "value": status_value,
            "hint": "Retrieval and business databases are ready" if status_value == "healthy" else "Check runtime storage and worker dependencies",
            "tone": status_tone,
        },
        {"label": "Stored Contents", "value": f"{content_count:,}", "hint": f"Chapters {chapter_count:,}", "tone": "blue"},
        {"label": "Semantic Chunks", "value": f"{semantic_chunk_count:,}", "hint": f"Evidence windows {evidence_window_count:,}", "tone": "cyan"},
        {"label": "Running Tasks", "value": str(running_tasks), "hint": f"Queued {queued_tasks} / Paused {paused_tasks} / Total {total_tasks}", "tone": "orange"},
        {"label": "Stored Results", "value": f"{result_count:,}", "hint": f"Reviews {review_count:,}", "tone": "blue"},
        {"label": "Fallback Results", "value": str(fallback_count), "hint": f"Failed or partial tasks {failure_tasks}", "tone": "orange"},
    ]

    services = [
        {
            "title": "Retrieval Database",
            "status": "ready" if retrieval_db_exists else "missing",
            "items": [
                ["database", Path(SETTINGS.db_path).name],
                ["chapters", f"{chapter_count:,}"],
                ["contents", f"{content_count:,}"],
                ["semantic chunks", f"{semantic_chunk_count:,}"],
            ],
        },
        {
            "title": "Business Store",
            "status": "ready" if business_db_exists else "missing",
            "items": [
                ["database", Path(SETTINGS.business_db_path).name],
                ["tasks", str(total_tasks)],
                ["results", f"{result_count:,}"],
                ["reviews", f"{review_count:,}"],
                ["SQLite lock events", str(db_lock_event_count)],
                ["max lock wait", f"{db_lock_wait_max:.3f}s"],
                ["skipped background writes", str(db_lock_skipped_count)],
            ],
        },
        {
            "title": "Semantic Backend",
            "status": "disabled" if semantic_backend_disabled else "configured",
            "items": [
                ["backend", semantic_backend_name or "disabled"],
                ["embedding provider", SETTINGS.semantic_remote_embedding_backend],
                ["model", SETTINGS.semantic_model],
                ["Qdrant", SETTINGS.semantic_qdrant_url],
                ["chunk collection", SETTINGS.semantic_chunk_collection],
            ],
        },
        {
            "title": "Runtime Paths",
            "status": "available",
            "items": [
                ["upload root", SETTINGS.task_upload_root],
                ["export root", SETTINGS.task_export_root],
                ["default creator", SETTINGS.task_created_by_default],
                ["allowed origins", ", ".join(SETTINGS.cors_allowed_origins_list or ["*"])],
            ],
        },
    ]

    recent_exceptions = [
        {
            "time": row["updated_at"] or row["created_at"] or "",
            "module": "task executor",
            "type": row["status"],
            "summary": row["error_message"] or row["status_message"] or "latest task update",
            "impact": row["task_id"],
        }
        for row in recent_tasks
        if row["status"] in {"failed", "partial_failed"}
    ]
    if not recent_exceptions:
        recent_exceptions.append(
            {
                "time": recent_tasks[0]["updated_at"] if recent_tasks else "",
                "module": "system monitor",
                "type": "normal",
                "summary": "No recent task failures. Runtime indicators are stable.",
                "impact": f"running {running_tasks} / queued {queued_tasks}",
            }
        )

    slow_tasks = [
        {
            "id": row["task_id"],
            "type": row["task_type"],
            "duration": _format_duration_seconds(row["started_at"], row["finished_at"]),
            "status": row["status"],
            "time": row["updated_at"] or row["created_at"] or "",
        }
        for row in recent_tasks
    ]

    return {
        "cards": cards,
        "services": services,
        "recent_exceptions": recent_exceptions,
        "slow_tasks": slow_tasks,
    }


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="lax")


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
    )


def _require_current_user(session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME)) -> dict[str, object]:
    if not session_id:
        raise HTTPException(status_code=401, detail="login required")
    user = get_session_user(SETTINGS.business_db_path, session_id)
    if user is None:
        raise HTTPException(status_code=401, detail="session expired")
    return user


def _with_task_queue_metadata(task: dict[str, object], *, task_kind: str) -> dict[str, object]:
    """Attach aggregate queue feedback without disclosing other users' task details."""
    enriched = dict(task)
    enriched["queue"] = get_task_queue_metadata(
        SETTINGS.business_db_path,
        task_kind=task_kind,
        task_id=str(task.get("task_id") or ""),
        worker_count=SETTINGS.auto_worker_count,
        seconds_per_item=SETTINGS.task_queue_eta_seconds_per_item,
    )
    return enriched


def _current_user_id(user: dict[str, object]) -> int:
    return int(user["user_id"])


def _require_admin_user(user: dict[str, object] = Depends(_require_current_user)) -> dict[str, object]:
    if str(user.get("role") or "") != "admin":
        raise HTTPException(status_code=403, detail="admin permission required")
    return user


app.include_router(
    build_cover_monitor_router(
        db_path=SETTINGS.cover_monitor_db_path,
        import_root=SETTINGS.cover_monitor_import_root,
        asset_root=SETTINGS.cover_monitor_asset_root,
        import_max_bytes=SETTINGS.cover_monitor_import_max_bytes,
        current_user_dependency=_require_current_user,
        vision_provider=SETTINGS.cover_monitor_vision_api_base,
        vision_model=SETTINGS.cover_monitor_vision_model,
    )
)


@app.post("/api/v1/auth/login", response_model=UserProfileResponse)
def login(body: LoginRequest, response: Response) -> UserProfileResponse:
    user = authenticate_user(
        SETTINGS.business_db_path,
        username=body.username,
        password=body.password,
    )
    if user is None:
        raise HTTPException(status_code=401, detail="username or password is invalid")
    session = create_user_session(
        SETTINGS.business_db_path,
        user_id=int(user["user_id"]),
        session_ttl_hours=SESSION_TTL_HOURS,
    )
    _set_session_cookie(response, str(session["session_id"]))
    return UserProfileResponse(user=user)


@app.post("/api/v1/auth/logout")
def logout(
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict[str, str]:
    if session_id:
        revoke_user_session(SETTINGS.business_db_path, session_id)
    _clear_session_cookie(response)
    return {"status": "ok"}


@app.get("/api/v1/auth/me", response_model=UserProfileResponse)
def get_me(user: dict[str, object] = Depends(_require_current_user)) -> UserProfileResponse:
    return UserProfileResponse(user=dict(user))


@app.get("/api/v1/users", response_model=UserListResponse)
def get_users(user: dict[str, object] = Depends(_require_admin_user)) -> UserListResponse:
    return UserListResponse(items=list_users(SETTINGS.business_db_path), current_user_id=_current_user_id(user))


@app.post("/api/v1/users", response_model=UserProfileResponse)
def create_user_api(
    body: CreateUserRequest,
    user: dict[str, object] = Depends(_require_admin_user),
) -> UserProfileResponse:
    try:
        created = create_user(
            SETTINGS.business_db_path,
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            role=body.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail="username already exists") from exc
    return UserProfileResponse(user=created)


@app.patch("/api/v1/users/{user_id}", response_model=UserProfileResponse)
def update_user_api(
    user_id: int,
    body: UpdateUserRequest,
    user: dict[str, object] = Depends(_require_admin_user),
) -> UserProfileResponse:
    try:
        updated = update_user(
            SETTINGS.business_db_path,
            user_id=user_id,
            display_name=body.display_name,
            role=body.role,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    return UserProfileResponse(user=updated)


@app.get("/api/v1/health", response_model=HealthResponse)
@app.get("/api/v1/system/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="novel-similarity-compare-api",
        version="0.1.0",
    )


@app.get("/api/v1/health/live", response_model=HealthResponse)
@app.get("/api/v1/health/livez", response_model=HealthResponse)
async def health_live() -> HealthResponse:
    """Return a dependency-free liveness response for the process watchdog."""
    return HealthResponse(
        status="ok",
        service="novel-similarity-compare-api",
        version="0.1.0",
    )


def _runtime_file_status(path_text: str) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return "ready" if path.is_file() else "missing"


@app.get("/api/v1/health/ready", response_model=ReadinessResponse)
async def health_ready(response: Response) -> ReadinessResponse:
    """Check local runtime prerequisites without probing external services."""
    checks = {
        "business_db": _runtime_file_status(SETTINGS.business_db_path),
        "novel_retrieval_db": _runtime_file_status(SETTINGS.db_path),
        "drama_subtitle_db": _runtime_file_status(SETTINGS.drama_subtitle_db_path),
    }
    required_checks = ("business_db", "novel_retrieval_db")
    ready = all(checks[name] == "ready" for name in required_checks)
    response.status_code = 200 if ready else 503
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        service="novel-similarity-compare-api",
        version="0.1.0",
        checks=checks,
    )


@app.post("/api/v1/compare/single", response_model=CompareSingleResponse)
def compare_single(
    body: CompareSingleRequest,
    user: dict[str, object] = Depends(_require_current_user),
) -> CompareSingleResponse:
    query_text = body.query_text.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="query_text is empty")

    started_at = time.perf_counter()
    try:
        payload = run_compare_pipeline(
            ComparePipelineRequest(
                db_path=SETTINGS.db_path,
                detection_mode=body.detection_mode,
                query_text=query_text,
                merged_top_k=body.merged_top_k,
                compare_top_k=body.compare_top_k,
                top_k=body.top_k,
                semantic_config=SETTINGS.build_semantic_config(merged_top_k=body.merged_top_k),
                candidate_display_score_threshold=(
                    body.candidate_display_score_threshold
                    if body.candidate_display_score_threshold is not None
                    else SETTINGS.candidate_display_score_threshold
                ),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return CompareSingleResponse(
        request_id=str(uuid4()),
        duration_seconds=round(time.perf_counter() - started_at, 4),
        payload=payload,
    )


@app.post("/api/v1/drama-subtitles/compare", response_model=DramaSubtitleCompareResponse)
def compare_drama_subtitles(
    body: DramaSubtitleCompareRequest,
    user: dict[str, object] = Depends(_require_current_user),
) -> DramaSubtitleCompareResponse:
    query_text = body.query_text.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="query_text is empty")

    started_at = time.perf_counter()
    try:
        payload = search_drama_subtitle_hybrid_candidates(
            db_path=SETTINGS.drama_subtitle_db_path,
            query_text=query_text,
            candidate_limit=body.top_k or 10,
            window_limit=body.window_limit or 200,
            include_window_text=True,
            language_code=body.language_code or "",
            semantic_enabled=(
                SETTINGS.drama_subtitle_semantic_enabled
                if body.semantic_enabled is None
                else body.semantic_enabled
            ),
            semantic_config=SETTINGS.build_drama_subtitle_semantic_config(),
            semantic_window_limit=body.semantic_window_limit or 100,
        )
        translation_config = SETTINGS.build_drama_subtitle_translation_config()
        if body.translation_fallback is not None:
            translation_config = type(translation_config)(
                **{**translation_config.__dict__, "enabled": bool(body.translation_fallback)}
            )
        payload = apply_translation_fallback(
            native_payload=payload,
            query_text=query_text,
            search_function=search_drama_subtitle_hybrid_candidates,
            search_kwargs={
                "db_path": SETTINGS.drama_subtitle_db_path,
                "candidate_limit": body.top_k or 10,
                "window_limit": body.window_limit or 200,
                "include_window_text": True,
                "semantic_enabled": (
                    SETTINGS.drama_subtitle_semantic_enabled
                    if body.semantic_enabled is None
                    else body.semantic_enabled
                ),
                "semantic_config": SETTINGS.build_drama_subtitle_semantic_config(),
                "semantic_window_limit": body.semantic_window_limit or 100,
            },
            config=translation_config,
            business_db_path=SETTINGS.business_db_path,
            owner_user_id=_current_user_id(user),
        )
    except (ValueError, DramaSubtitleRetrievalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return DramaSubtitleCompareResponse(
        request_id=str(uuid4()),
        duration_seconds=round(time.perf_counter() - started_at, 4),
        payload=payload,
    )


@app.post("/api/v1/drama-subtitles/video-compare", response_model=DramaSubtitleVideoCompareResponse)
def compare_drama_subtitle_video_api(
    body: DramaSubtitleVideoCompareRequest,
    user: dict[str, object] = Depends(_require_current_user),
) -> DramaSubtitleVideoCompareResponse:
    # Translation cache entries are isolated per authenticated user.
    owner_user_id = _current_user_id(user)
    started_at = time.perf_counter()
    try:
        payload = compare_drama_subtitle_video(
            db_path=SETTINGS.drama_subtitle_db_path,
            query_text=body.query_text,
            cues=[cue.model_dump() for cue in body.cues],
            candidate_limit=body.top_k or 10,
            window_limit=body.window_limit or 200,
            language_code=body.language_code or "",
            semantic_enabled=(
                SETTINGS.drama_subtitle_semantic_enabled
                if body.semantic_enabled is None
                else body.semantic_enabled
            ),
            semantic_config=SETTINGS.build_drama_subtitle_semantic_config(),
            semantic_window_limit=body.semantic_window_limit or 100,
        )
        native_decision = payload.get("video_decision") if isinstance(payload.get("video_decision"), dict) else {}
        fast_screen = payload.get("fast_screen") if isinstance(payload.get("fast_screen"), dict) else {}
        translation_config = SETTINGS.build_drama_subtitle_translation_config()
        if body.translation_fallback is not None:
            translation_config = type(translation_config)(
                **{**translation_config.__dict__, "enabled": bool(body.translation_fallback)}
            )
        translated_payload = apply_translation_fallback(
            native_payload={
                "query_text": body.query_text,
                "query_language_code": fast_screen.get("query_language_code") or body.language_code or "unknown",
                "decision": native_decision,
                "candidates": fast_screen.get("candidates") or [],
            },
            query_text=body.query_text,
            search_function=search_drama_subtitle_hybrid_candidates,
            search_kwargs={
                "db_path": SETTINGS.drama_subtitle_db_path,
                "candidate_limit": body.top_k or 10,
                "window_limit": body.window_limit or 200,
                "include_window_text": True,
                "semantic_enabled": (
                    SETTINGS.drama_subtitle_semantic_enabled
                    if body.semantic_enabled is None
                    else body.semantic_enabled
                ),
                "semantic_config": SETTINGS.build_drama_subtitle_semantic_config(),
                "semantic_window_limit": body.semantic_window_limit or 100,
            },
            config=translation_config,
            business_db_path=SETTINGS.business_db_path,
            owner_user_id=owner_user_id,
        )
        payload["translation_fallback"] = translated_payload.get("translation_fallback")
        if str(translated_payload.get("decision", {}).get("outcome") or "") == "translation_assisted_match":
            payload["translation_candidates"] = translated_payload.get("candidates") or []
            payload["translated_query_text"] = translated_payload.get("translated_query_text") or ""
            payload["video_decision"] = {
                **translated_payload["decision"],
                "decision_scope": "video",
                "decision_source": "translation_fallback",
                "matched_segment_order": None,
                "video_reason": "native_no_match_translation_candidate",
            }
    except (ValueError, DramaSubtitleRetrievalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return DramaSubtitleVideoCompareResponse(
        request_id=str(uuid4()),
        duration_seconds=round(time.perf_counter() - started_at, 4),
        payload=payload,
    )


@app.get("/api/v1/drama-subtitles/evidence-context/{window_uid}")
def get_drama_subtitle_evidence_context_api(
    window_uid: str,
    context_chars: int = Query(default=2400, ge=600, le=5000),
    before_lines: int = Query(default=6, ge=0, le=30),
    user: dict[str, object] = Depends(_require_current_user),
) -> dict[str, object]:
    del user  # The subtitle corpus is shared; authentication gates review access.
    try:
        context = get_drama_subtitle_evidence_context(
            db_path=SETTINGS.drama_subtitle_db_path,
            window_uid=window_uid,
            context_chars=context_chars,
            before_lines=before_lines,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DramaSubtitleEvidenceContextError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"context": context}


@app.post("/api/v1/drama-subtitles/tasks", response_model=DramaSubtitleTaskCreateResponse)
async def create_drama_subtitle_task_api(
    file: UploadFile = File(...),
    top_k: Optional[int] = Form(default=None),
    window_limit: Optional[int] = Form(default=None),
    semantic_enabled: Optional[bool] = Form(default=None),
    semantic_window_limit: Optional[int] = Form(default=None),
    translation_fallback: Optional[bool] = Form(default=None),
    user: dict[str, object] = Depends(_require_current_user),
) -> DramaSubtitleTaskCreateResponse:
    task_id = str(uuid4())
    target_path, file_size, file_sha256 = await _save_upload_file(task_id, file)
    parsed_inputs = parse_task_input_file(target_path)
    if not parsed_inputs:
        raise HTTPException(status_code=400, detail="上传文件中没有可执行的字幕文本")
    task = create_drama_subtitle_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        source_file_name=Path(file.filename or "drama_subtitle_input.xlsx").name,
        source_file_ext=Path(file.filename or "drama_subtitle_input.xlsx").suffix.lower(),
        source_file_path=str(target_path),
        source_file_sha256=file_sha256,
        source_file_size=file_size,
        owner_user_id=_current_user_id(user),
        created_by=str(user.get("username") or SETTINGS.task_created_by_default),
        accepted_input_count=len(parsed_inputs),
        params={
            "top_k": top_k or 10,
            "window_limit": window_limit or 200,
            "semantic_enabled": (
                SETTINGS.drama_subtitle_semantic_enabled
                if semantic_enabled is None
                else semantic_enabled
            ),
            "semantic_window_limit": semantic_window_limit or 100,
            "translation_fallback": (
                SETTINGS.drama_subtitle_translation_enabled
                if translation_fallback is None
                else translation_fallback
            ),
        },
    )
    return DramaSubtitleTaskCreateResponse(
        task_id=str(task["task_id"]),
        status=str(task["status"]),
        source_file_name=str(task["source_file_name"]),
    )


@app.get("/api/v1/drama-subtitles/tasks", response_model=DramaSubtitleTaskListResponse)
def get_drama_subtitle_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict[str, object] = Depends(_require_current_user),
) -> DramaSubtitleTaskListResponse:
    return DramaSubtitleTaskListResponse(
        items=[
            _with_task_queue_metadata(task, task_kind="drama_subtitle")
            for task in list_drama_subtitle_tasks(
                SETTINGS.business_db_path,
                owner_user_id=_current_user_id(user),
                limit=limit,
                offset=offset,
            )
        ],
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/drama-subtitles/tasks/{task_id}", response_model=DramaSubtitleTaskDetailResponse)
def get_drama_subtitle_task_api(
    task_id: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> DramaSubtitleTaskDetailResponse:
    task = get_drama_subtitle_task(
        SETTINGS.business_db_path,
        task_id,
        owner_user_id=_current_user_id(user),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="subtitle task not found")
    return DramaSubtitleTaskDetailResponse(
        task=_with_task_queue_metadata(task, task_kind="drama_subtitle"),
        items=list_drama_subtitle_task_items(SETTINGS.business_db_path, task_id),
    )


@app.post("/api/v1/drama-subtitles/tasks/items/{task_item_id}/review", response_model=TaskResultResponse)
def save_drama_subtitle_task_review_api(
    task_item_id: int,
    body: CompareReviewRequest,
    user: dict[str, object] = Depends(_require_current_user),
) -> TaskResultResponse:
    try:
        item = upsert_drama_subtitle_task_review(
            db_path=SETTINGS.business_db_path,
            task_item_id=task_item_id,
            review_status=body.review_status,
            reviewer_name=str(user.get("display_name") or user.get("username") or ""),
            review_note=body.review_note,
            owner_user_id=_current_user_id(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="subtitle task item not found")
    return TaskResultResponse(result=item)


@app.get("/api/v1/drama-subtitles/tasks/{task_id}/exports/review-xlsx")
def download_drama_subtitle_review_export(
    task_id: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> FileResponse:
    try:
        export_path, export_filename = build_drama_subtitle_review_export_xlsx(
            business_db_path=SETTINGS.business_db_path,
            export_root=SETTINGS.task_export_root,
            owner_user_id=_current_user_id(user),
            task_id=task_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path=export_path,
        filename=export_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/v1/drama-subtitles/tasks/{task_id}/pause", response_model=BasicTaskResponse)
def pause_drama_subtitle_task_api(
    task_id: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> BasicTaskResponse:
    try:
        task = update_drama_subtitle_task_control(
            db_path=SETTINGS.business_db_path, task_id=task_id, action="pause", owner_user_id=_current_user_id(user)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="subtitle task not found")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/drama-subtitles/tasks/{task_id}/resume", response_model=BasicTaskResponse)
def resume_drama_subtitle_task_api(
    task_id: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> BasicTaskResponse:
    try:
        task = update_drama_subtitle_task_control(
            db_path=SETTINGS.business_db_path, task_id=task_id, action="resume", owner_user_id=_current_user_id(user)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="subtitle task not found")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/drama-subtitles/tasks/{task_id}/cancel", response_model=BasicTaskResponse)
def cancel_drama_subtitle_task_api(
    task_id: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> BasicTaskResponse:
    try:
        task = update_drama_subtitle_task_control(
            db_path=SETTINGS.business_db_path, task_id=task_id, action="cancel", owner_user_id=_current_user_id(user)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="subtitle task not found")
    return BasicTaskResponse(task=task)


@app.delete("/api/v1/drama-subtitles/tasks/{task_id}", response_model=BasicTaskResponse)
def delete_drama_subtitle_task_api(
    task_id: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> BasicTaskResponse:
    try:
        task = update_drama_subtitle_task_control(
            db_path=SETTINGS.business_db_path, task_id=task_id, action="delete", owner_user_id=_current_user_id(user)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="subtitle task not found")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/tasks", response_model=TaskCreateResponse)
async def create_task(
    file: UploadFile = File(...),
    detection_mode: str = Form(...),
    top_k: Optional[int] = Form(default=None),
    compare_top_k: Optional[int] = Form(default=None),
    merged_top_k: Optional[int] = Form(default=None),
    candidate_display_score_threshold: Optional[float] = Form(default=None, ge=0.0, le=1.0),
    user: dict[str, object] = Depends(_require_current_user),
) -> TaskCreateResponse:
    if detection_mode not in {"reuse", "rewrite"}:
        raise HTTPException(status_code=400, detail="Unsupported detection_mode")

    task_id = str(uuid4())
    target_path, file_size, file_sha256 = await _save_upload_file(task_id, file)
    parsed_inputs = parse_task_input_file(target_path)
    if not parsed_inputs:
        raise HTTPException(status_code=400, detail="上传文件中没有可执行的待检测文本")
    task = create_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        detection_mode=detection_mode,
        source_file_name=Path(file.filename or "batch_input.txt").name,
        source_file_ext=Path(file.filename or "batch_input.txt").suffix.lower(),
        source_file_path=str(target_path),
        source_file_sha256=file_sha256,
        source_file_size=file_size,
        owner_user_id=_current_user_id(user),
        params={
            "top_k": top_k,
            "compare_top_k": compare_top_k,
            "merged_top_k": merged_top_k,
            "candidate_display_score_threshold": (
                candidate_display_score_threshold
                if candidate_display_score_threshold is not None
                else SETTINGS.candidate_display_score_threshold
            ),
        },
        created_by=str(user.get("username") or SETTINGS.task_created_by_default),
        accepted_input_count=len(parsed_inputs),
    )
    return TaskCreateResponse(
        task_id=task["task_id"],
        status=task["status"],
        detection_mode=task["detection_mode"],
        source_file_name=task["source_file_name"],
    )


@app.get("/api/v1/tasks", response_model=TaskListResponse)
def get_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict[str, object] = Depends(_require_current_user),
) -> TaskListResponse:
    items = list_compare_tasks(
        db_path=SETTINGS.business_db_path,
        limit=limit,
        offset=offset,
        owner_user_id=_current_user_id(user),
    )
    return TaskListResponse(
        items=[_with_task_queue_metadata(task, task_kind="compare") for task in items],
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task_detail(
    task_id: str,
    item_limit: int = Query(default=20, ge=1, le=200),
    item_offset: int = Query(default=0, ge=0),
    user: dict[str, object] = Depends(_require_current_user),
) -> TaskDetailResponse:
    current_user_id = _current_user_id(user)
    task = get_compare_task(SETTINGS.business_db_path, task_id, owner_user_id=current_user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    task = _with_task_queue_metadata(task, task_kind="compare")
    items = list_compare_task_items(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        limit=item_limit,
        offset=item_offset,
        owner_user_id=current_user_id,
    )
    result_stats = get_compare_task_item_stats(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        owner_user_id=current_user_id,
    )
    return TaskDetailResponse(
        task=task,
        items=items,
        item_limit=item_limit,
        item_offset=item_offset,
        item_total=int(result_stats["item_total"]),
        result_stats=result_stats,
    )


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=BasicTaskResponse)
def cancel_task(task_id: str, user: dict[str, object] = Depends(_require_current_user)) -> BasicTaskResponse:
    task = cancel_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        reason="task cancelled from api",
        owner_user_id=_current_user_id(user),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found or cannot be cancelled")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/tasks/{task_id}/pause", response_model=BasicTaskResponse)
def pause_task(task_id: str, user: dict[str, object] = Depends(_require_current_user)) -> BasicTaskResponse:
    task = pause_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        reason="task paused from api",
        owner_user_id=_current_user_id(user),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found or cannot be paused")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/tasks/{task_id}/resume", response_model=BasicTaskResponse)
def resume_task(task_id: str, user: dict[str, object] = Depends(_require_current_user)) -> BasicTaskResponse:
    task = resume_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        reason="task resumed from api",
        owner_user_id=_current_user_id(user),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found or cannot be resumed")
    return BasicTaskResponse(task=task)


@app.delete("/api/v1/tasks/{task_id}", response_model=BasicTaskResponse)
def delete_task(task_id: str, user: dict[str, object] = Depends(_require_current_user)) -> BasicTaskResponse:
    task = delete_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        reason="task deleted from api",
        owner_user_id=_current_user_id(user),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found or cannot be deleted")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/tasks/{task_id}/retry", response_model=TaskCreateResponse)
def retry_task(task_id: str, user: dict[str, object] = Depends(_require_current_user)) -> TaskCreateResponse:
    current_user_id = _current_user_id(user)
    task = get_compare_task(SETTINGS.business_db_path, task_id, owner_user_id=current_user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] not in {"failed", "partial_failed", "cancelled"}:
        raise HTTPException(status_code=400, detail="task status does not support retry")

    new_task_id = str(uuid4())
    retried = retry_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        new_task_id=new_task_id,
        created_by=str(user.get("username") or "api-retry"),
        owner_user_id=current_user_id,
    )
    if retried is None:
        raise HTTPException(status_code=500, detail="failed to create retry task")
    return TaskCreateResponse(
        task_id=retried["task_id"],
        status=retried["status"],
        detection_mode=retried["detection_mode"],
        source_file_name=retried["source_file_name"],
    )


@app.get("/api/v1/results/{result_id}", response_model=TaskResultResponse)
def get_result_detail(result_id: int, user: dict[str, object] = Depends(_require_current_user)) -> TaskResultResponse:
    result = get_compare_result(
        SETTINGS.business_db_path,
        result_id,
        retrieval_db_path=SETTINGS.db_path,
        owner_user_id=_current_user_id(user),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="result not found")
    return TaskResultResponse(result=result)


@app.get("/api/v1/results", response_model=ResultListResponse)
def get_results(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    task_id: str = Query(default=""),
    status: str = Query(default=""),
    review_status: str = Query(default=""),
    sort_by: str = Query(default="updated_at_desc"),
    dedupe_latest: bool = Query(default=False),
    q: str = Query(default=""),
    exclude_cleared: bool = Query(default=False),
    candidate_score_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    user: dict[str, object] = Depends(_require_current_user),
) -> ResultListResponse:
    current_user_id = _current_user_id(user)
    items = list_compare_results(
        db_path=SETTINGS.business_db_path,
        limit=limit,
        offset=offset,
        task_id=task_id,
        item_status=status,
        review_status=review_status,
        sort_by=sort_by,
        dedupe_latest=dedupe_latest,
        owner_user_id=current_user_id,
        text_filter=q,
        exclude_cleared=exclude_cleared,
        candidate_score_threshold=candidate_score_threshold,
    )
    stats = summarize_compare_results(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        item_status=status,
        review_status=review_status,
        dedupe_latest=dedupe_latest,
        owner_user_id=current_user_id,
        text_filter=q,
        exclude_cleared=exclude_cleared,
        candidate_score_threshold=candidate_score_threshold,
    )
    total = int(stats.get("total") or 0)
    return ResultListResponse(items=items, limit=limit, offset=offset, total=total, stats=stats)


@app.post("/api/v1/results/{result_id}/review", response_model=TaskResultResponse)
def save_result_review(
    result_id: int,
    body: CompareReviewRequest,
    user: dict[str, object] = Depends(_require_current_user),
) -> TaskResultResponse:
    review_status = body.review_status.strip()
    if not review_status:
        raise HTTPException(status_code=400, detail="review_status is empty")
    result = upsert_compare_task_review(
        db_path=SETTINGS.business_db_path,
        result_id=result_id,
        review_status=review_status,
        reviewer_name=str(user.get("display_name") or user.get("username") or "").strip(),
        review_note=body.review_note.strip(),
        owner_user_id=_current_user_id(user),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="result not found")
    return TaskResultResponse(result=result)


@app.post("/api/v1/results/clear-pending", response_model=BulkActionResponse)
def clear_pending_results(user: dict[str, object] = Depends(_require_current_user)) -> BulkActionResponse:
    affected_count = clear_pending_review_results(
        db_path=SETTINGS.business_db_path,
        owner_user_id=_current_user_id(user),
    )
    return BulkActionResponse(
        status="ok",
        affected_count=affected_count,
        message="pending review results cleared",
    )


@app.get("/api/v1/results/exports/review-xlsx")
def download_review_export(
    task_id: str = Query(default=""),
    status: str = Query(default=""),
    review_status: str = Query(default=""),
    sort_by: str = Query(default="updated_at_desc"),
    dedupe_latest: bool = Query(default=False),
    q: str = Query(default=""),
    candidate_score_threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    user: dict[str, object] = Depends(_require_current_user),
) -> FileResponse:
    current_user_id = _current_user_id(user)
    export_path, export_filename = build_review_export_xlsx(
        business_db_path=SETTINGS.business_db_path,
        retrieval_db_path=SETTINGS.db_path,
        export_root=SETTINGS.task_export_root,
        owner_user_id=current_user_id,
        task_id=task_id.strip(),
        item_status=status.strip(),
        review_status=review_status.strip(),
        sort_by=sort_by.strip() or "updated_at_desc",
        dedupe_latest=dedupe_latest,
        text_filter=q.strip(),
        processed_only=True,
        candidate_score_threshold=candidate_score_threshold,
    )
    return FileResponse(
        path=export_path,
        filename=export_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/v1/tasks/{task_id}/exports/{export_kind}")
def download_task_export(
    task_id: str,
    export_kind: str,
    user: dict[str, object] = Depends(_require_current_user),
) -> FileResponse:
    task = get_compare_task(SETTINGS.business_db_path, task_id, owner_user_id=_current_user_id(user))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    export_path_map = {
        "summary": task["summary_export_path"],
        "review": task["review_export_path"],
        "json": task["result_json_path"],
    }
    if export_kind not in export_path_map:
        raise HTTPException(status_code=400, detail="unsupported export kind")

    export_path = _resolve_runtime_path(export_path_map[export_kind])
    if export_path is None or not export_path.exists():
        raise HTTPException(status_code=404, detail="export file not found")

    media_type = {
        "summary": "text/csv",
        "review": "text/csv",
        "json": "application/json",
    }[export_kind]
    return FileResponse(path=export_path, filename=export_path.name, media_type=media_type)


@app.get("/api/v1/system/status", response_model=SystemStatusResponse)
def get_system_status(user: dict[str, object] = Depends(_require_current_user)) -> SystemStatusResponse:
    return SystemStatusResponse(payload=_build_system_status_payload())
