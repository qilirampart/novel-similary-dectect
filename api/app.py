from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api.config import SETTINGS
from api.runtime_worker import start_auto_worker, stop_auto_worker
from api.schemas import (
    BasicTaskResponse,
    CompareReviewRequest,
    CompareSingleRequest,
    CompareSingleResponse,
    HealthResponse,
    ResultListResponse,
    SystemStatusResponse,
    TaskCreateResponse,
    TaskDetailResponse,
    TaskListResponse,
    TaskResultResponse,
)
from service.business_store import (
    cancel_compare_task,
    connect_business_db,
    create_compare_task,
    get_compare_result,
    get_compare_task,
    init_business_db,
    list_compare_results,
    list_compare_task_items,
    list_compare_tasks,
    retry_compare_task,
    upsert_compare_task_review,
)
from service.compare_pipeline import ComparePipelineRequest, run_compare_pipeline

ROOT_DIR = Path(__file__).resolve().parent.parent


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

    chapter_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM chapters")
    content_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM chapter_contents")
    evidence_window_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM evidence_windows")
    semantic_chunk_count = _scalar_query(SETTINGS.db_path, "SELECT COUNT(*) FROM semantic_chunks")

    total_tasks = _scalar_query(SETTINGS.business_db_path, "SELECT COUNT(*) FROM compare_tasks")
    running_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE status = 'running'",
    )
    queued_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE status = 'queued'",
    )
    failure_tasks = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_tasks WHERE status IN ('failed', 'partial_failed')",
    )
    result_count = _scalar_query(SETTINGS.business_db_path, "SELECT COUNT(*) FROM compare_task_items")
    review_count = _scalar_query(SETTINGS.business_db_path, "SELECT COUNT(*) FROM compare_task_reviews")
    fallback_count = _scalar_query(
        SETTINGS.business_db_path,
        "SELECT COUNT(*) FROM compare_task_items WHERE semantic_status = 'fallback_lexical_only'",
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
        {"label": "Running Tasks", "value": str(running_tasks), "hint": f"Queued {queued_tasks} / Total {total_tasks}", "tone": "orange"},
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
            "status": "configured" if SETTINGS.semantic_backend else "disabled",
            "items": [
                ["backend", SETTINGS.semantic_remote_embedding_backend],
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


@app.get("/api/v1/health", response_model=HealthResponse)
@app.get("/api/v1/system/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="novel-similarity-compare-api",
        version="0.1.0",
    )


@app.post("/api/v1/compare/single", response_model=CompareSingleResponse)
def compare_single(body: CompareSingleRequest) -> CompareSingleResponse:
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
    created_by: str = Form(default=""),
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
        created_by=created_by or SETTINGS.task_created_by_default,
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
) -> TaskListResponse:
    items = list_compare_tasks(
        db_path=SETTINGS.business_db_path,
        limit=limit,
        offset=offset,
    )
    return TaskListResponse(items=items, limit=limit, offset=offset)


@app.get("/api/v1/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task_detail(
    task_id: str,
    item_limit: int = Query(default=20, ge=1, le=200),
    item_offset: int = Query(default=0, ge=0),
) -> TaskDetailResponse:
    task = get_compare_task(SETTINGS.business_db_path, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    items = list_compare_task_items(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        limit=item_limit,
        offset=item_offset,
    )
    return TaskDetailResponse(
        task=task,
        items=items,
        item_limit=item_limit,
        item_offset=item_offset,
    )


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=BasicTaskResponse)
def cancel_task(task_id: str) -> BasicTaskResponse:
    task = cancel_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        reason="task cancelled from api",
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found or cannot be cancelled")
    return BasicTaskResponse(task=task)


@app.post("/api/v1/tasks/{task_id}/retry", response_model=TaskCreateResponse)
def retry_task(task_id: str) -> TaskCreateResponse:
    task = get_compare_task(SETTINGS.business_db_path, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] not in {"failed", "partial_failed", "cancelled"}:
        raise HTTPException(status_code=400, detail="task status does not support retry")

    new_task_id = str(uuid4())
    retried = retry_compare_task(
        db_path=SETTINGS.business_db_path,
        task_id=task_id,
        new_task_id=new_task_id,
        created_by="api-retry",
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
def get_result_detail(result_id: int) -> TaskResultResponse:
    result = get_compare_result(SETTINGS.business_db_path, result_id)
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
) -> ResultListResponse:
    items = list_compare_results(
        db_path=SETTINGS.business_db_path,
        limit=limit,
        offset=offset,
        task_id=task_id,
        item_status=status,
        review_status=review_status,
        sort_by=sort_by,
    )
    return ResultListResponse(items=items, limit=limit, offset=offset)


@app.post("/api/v1/results/{result_id}/review", response_model=TaskResultResponse)
def save_result_review(result_id: int, body: CompareReviewRequest) -> TaskResultResponse:
    review_status = body.review_status.strip()
    if not review_status:
        raise HTTPException(status_code=400, detail="review_status is empty")
    result = upsert_compare_task_review(
        db_path=SETTINGS.business_db_path,
        result_id=result_id,
        review_status=review_status,
        reviewer_name=body.reviewer_name.strip(),
        review_note=body.review_note.strip(),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="result not found")
    return TaskResultResponse(result=result)


@app.get("/api/v1/tasks/{task_id}/exports/{export_kind}")
def download_task_export(task_id: str, export_kind: str) -> FileResponse:
    task = get_compare_task(SETTINGS.business_db_path, task_id)
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
def get_system_status() -> SystemStatusResponse:
    return SystemStatusResponse(payload=_build_system_status_payload())
