from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
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
from api.runtime_worker import start_auto_worker, stop_auto_worker
from api.schemas import (
    BasicTaskResponse,
    BulkActionResponse,
    CompareReviewRequest,
    CompareSingleRequest,
    CompareSingleResponse,
    CreateUserRequest,
    HealthResponse,
    LoginRequest,
    ResultListResponse,
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
from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline
from service.review_export import build_review_export_xlsx

ROOT_DIR = Path(__file__).resolve().parent.parent
SEMANTIC_DISABLED_BACKENDS = {"", "0", "false", "off", "none", "disabled"}
SESSION_COOKIE_NAME = "novel_similarity_session"
SESSION_TTL_HOURS = 12


@asynccontextmanager
async def lifespan(app: FastAPI):
    SETTINGS.ensure_runtime_dirs()
    init_business_db(SETTINGS.business_db_path)
    app.state.auto_worker_handle = start_auto_worker()
    try:
        yield
    finally:
        stop_auto_worker(getattr(app.state, "auto_worker_handle", None))
        app.state.auto_worker_handle = None


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
           AND i.semantic_status = 'fallback_lexical_only'
        """,
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


def _current_user_id(user: dict[str, object]) -> int:
    return int(user["user_id"])


def _require_admin_user(user: dict[str, object] = Depends(_require_current_user)) -> dict[str, object]:
    if str(user.get("role") or "") != "admin":
        raise HTTPException(status_code=403, detail="admin permission required")
    return user


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
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="novel-similarity-compare-api",
        version="0.1.0",
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
    return TaskListResponse(items=items, limit=limit, offset=offset)


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
