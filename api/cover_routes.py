from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import BadZipFile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, Field, PositiveInt

from service.cover_monitor.prompts import PROMPT_VERSION
from service.cover_monitor.store import CoverAccessScope, CoverMonitorStore


INTERNAL_WORKSPACE_KEY = "internal"


class CoverRunSummary(BaseModel):
    run_id: str
    status: str
    trigger_type: str
    intensity: str
    total_item_count: int
    completed_item_count: int
    failed_item_count: int
    total_channel_count: int = 0
    status_message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class CoverOverviewResponse(BaseModel):
    channel_count: int
    video_count: int
    risk_count: int
    pending_review_count: int
    risk_distribution: dict[str, int]
    latest_run: Optional[CoverRunSummary] = None


class CoverRunCreateRequest(BaseModel):
    intensity: Literal["conservative", "standard", "strict"] = "standard"
    channel_pks: list[PositiveInt] = Field(default_factory=list, max_length=5000)
    include_shorts: bool = True
    force_refresh: bool = False
    max_items_per_scope: int = Field(default=0, ge=0, le=10_000)


class CoverRunListResponse(BaseModel):
    items: list[CoverRunSummary]
    total: int
    limit: int
    offset: int


class CoverRunChannelSummary(BaseModel):
    run_channel_id: int
    channel_pk: int
    channel_name: str
    source_url: str
    scan_status: str
    completeness: str
    discovered_count: int
    error_message: Optional[str] = None


class CoverRunItemSummary(BaseModel):
    task_item_id: int
    video_pk: int
    video_id: str
    video_title: str
    video_url: str
    thumbnail_url: str
    reason: str
    stage: str
    status: str
    attempts: int
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    overall_risk: Optional[str] = None
    confidence: Optional[float] = None
    summary: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class CoverRunDetailResponse(BaseModel):
    run: CoverRunSummary
    channels: list[CoverRunChannelSummary]
    items: list[CoverRunItemSummary]
    item_total: int
    item_limit: int
    item_offset: int


class CoverRiskCaseSummary(BaseModel):
    case_id: str
    video_pk: int
    video_id: str
    video_title: str
    video_url: str
    thumbnail_url: str
    current_status: str
    opened_detection_id: str
    opened_risk: str
    opened_summary: str
    opened_evidence: str
    opened_confidence: float
    opened_asset_id: str
    latest_event_type: Optional[str] = None
    opened_at: str
    updated_at: str
    closed_at: Optional[str] = None


class CoverRiskCaseListResponse(BaseModel):
    items: list[CoverRiskCaseSummary]
    total: int
    limit: int
    offset: int


class CoverRiskCaseEvent(BaseModel):
    case_event_id: int
    detection_id: Optional[str] = None
    event_type: str
    actor_type: str
    actor_user_id: Optional[int] = None
    reason: str
    created_at: str
    overall_risk: Optional[str] = None
    risk_tags: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    evidence: Optional[str] = None
    confidence: Optional[float] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    duration_seconds: Optional[float] = None
    asset_id: Optional[str] = None
    content_sha256: Optional[str] = None
    storage_key: Optional[str] = None
    original_url: Optional[str] = None
    fetched_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fetched_at: Optional[str] = None


class CoverRiskCaseReview(BaseModel):
    case_review_id: int
    detection_id: Optional[str] = None
    action: str
    reason: str
    reviewed_by_user_id: int
    reviewed_at: str


class CoverRiskCaseDetailResponse(BaseModel):
    case: CoverRiskCaseSummary
    events: list[CoverRiskCaseEvent]
    reviews: list[CoverRiskCaseReview]


class CoverRiskCaseReviewRequest(BaseModel):
    action: Literal["confirm_rectified", "false_positive", "keep_open", "mark_unavailable", "reopen"]
    reason: str = Field(min_length=2, max_length=1000)


class CoverImportResponse(BaseModel):
    import_id: str
    import_kind: str
    status: str
    source_file_name: str
    source_file_sha256: str
    sheet_name: Optional[str] = None
    mapping: dict[str, str]
    stats: dict[str, int]
    error_message: Optional[str] = None
    conflict_samples: list[dict[str, Any]]
    created_at: str
    updated_at: str
    confirmed_at: Optional[str] = None
    finished_at: Optional[str] = None


def build_cover_monitor_router(
    *,
    db_path: str,
    import_root: str = "runtime/cover_monitor/imports",
    asset_root: str = "runtime/cover_monitor/assets",
    import_max_bytes: int = 150 * 1024 * 1024,
    current_user_dependency: Callable[..., dict[str, Any]],
    vision_provider: str = "",
    vision_model: str = "",
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/cover-monitor", tags=["cover-monitor"])
    store = CoverMonitorStore(db_path)

    @router.get("/overview", response_model=CoverOverviewResponse)
    def get_overview(
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverOverviewResponse:
        scope = CoverAccessScope(
            workspace_key=INTERNAL_WORKSPACE_KEY,
            user_id=int(user["user_id"]),
        )
        return CoverOverviewResponse.model_validate(store.get_overview(scope))

    def access_scope(user: dict[str, Any]) -> CoverAccessScope:
        return CoverAccessScope(
            workspace_key=INTERNAL_WORKSPACE_KEY,
            user_id=int(user["user_id"]),
        )

    def model_snapshot() -> dict[str, str]:
        raw_provider = str(vision_provider or "").strip()
        parsed = urlsplit(raw_provider)
        provider = parsed.hostname or raw_provider
        return {
            "provider": provider[:200],
            "model": str(vision_model or "unconfigured")[:200],
        }

    @router.post("/runs", response_model=CoverRunSummary)
    def create_run(
        body: CoverRunCreateRequest,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunSummary:
        try:
            result = store.create_run(
                access_scope(user),
                trigger_type="manual",
                intensity=body.intensity,
                channel_pks=[int(value) for value in body.channel_pks],
                params={
                    "include_shorts": body.include_shorts,
                    "force_refresh": body.force_refresh,
                    "max_items_per_scope": body.max_items_per_scope,
                },
                model_snapshot=model_snapshot(),
                prompt_version=PROMPT_VERSION,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return CoverRunSummary.model_validate(result)

    @router.get("/runs", response_model=CoverRunListResponse)
    def list_runs(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunListResponse:
        return CoverRunListResponse.model_validate(
            store.list_runs(access_scope(user), limit=limit, offset=offset)
        )

    @router.get("/runs/{run_id}", response_model=CoverRunSummary)
    def get_run(
        run_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunSummary:
        result = store.get_run(access_scope(user), run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="巡检批次不存在")
        return CoverRunSummary.model_validate(result)

    @router.get("/runs/{run_id}/detail", response_model=CoverRunDetailResponse)
    def get_run_detail(
        run_id: str,
        item_limit: int = Query(default=50, ge=1, le=100),
        item_offset: int = Query(default=0, ge=0),
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunDetailResponse:
        result = store.get_run_detail(
            access_scope(user),
            run_id,
            item_limit=item_limit,
            item_offset=item_offset,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="巡检批次不存在")
        return CoverRunDetailResponse.model_validate(result)

    def control_run(action: str, run_id: str, user: dict[str, Any]) -> CoverRunSummary:
        try:
            if action == "pause":
                result = store.request_pause(access_scope(user), run_id)
            elif action == "resume":
                result = store.resume_run(access_scope(user), run_id)
            else:
                result = store.request_cancel(access_scope(user), run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="巡检批次不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return CoverRunSummary.model_validate(result)

    @router.post("/runs/{run_id}/pause", response_model=CoverRunSummary)
    def pause_run(
        run_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunSummary:
        return control_run("pause", run_id, user)

    @router.post("/runs/{run_id}/resume", response_model=CoverRunSummary)
    def resume_run(
        run_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunSummary:
        return control_run("resume", run_id, user)

    @router.post("/runs/{run_id}/cancel", response_model=CoverRunSummary)
    def cancel_run(
        run_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRunSummary:
        return control_run("cancel", run_id, user)

    @router.get("/risk-cases", response_model=CoverRiskCaseListResponse)
    def list_risk_cases(
        status: Optional[Literal[
            "open",
            "needs_review",
            "confirmed_rectified",
            "false_positive",
            "unavailable",
            "closed",
        ]] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRiskCaseListResponse:
        return CoverRiskCaseListResponse.model_validate(
            store.list_risk_cases(
                access_scope(user),
                status=status or "",
                limit=limit,
                offset=offset,
            )
        )

    @router.get("/risk-cases/{case_id}", response_model=CoverRiskCaseDetailResponse)
    def get_risk_case(
        case_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRiskCaseDetailResponse:
        result = store.get_risk_case_detail(access_scope(user), case_id)
        if result is None:
            raise HTTPException(status_code=404, detail="封面风险案件不存在")
        return CoverRiskCaseDetailResponse.model_validate(result)

    @router.get("/risk-cases/{case_id}/assets/{asset_id}", response_class=FileResponse)
    def get_risk_case_asset(
        case_id: str,
        asset_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> FileResponse:
        asset = store.get_risk_case_asset(access_scope(user), case_id, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="封面证据不存在")
        root = Path(asset_root).resolve()
        target = (root / str(asset["storage_key"])).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="封面证据不存在") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="封面证据文件缺失")
        return FileResponse(
            path=target,
            media_type=str(asset["mime_type"]),
            filename=target.name,
        )

    @router.post("/risk-cases/{case_id}/review", response_model=CoverRiskCaseDetailResponse)
    def review_risk_case(
        case_id: str,
        body: CoverRiskCaseReviewRequest,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverRiskCaseDetailResponse:
        scope = access_scope(user)
        try:
            store.review_risk_case(
                scope,
                case_id,
                action=body.action,
                reason=body.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="封面风险案件不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result = store.get_risk_case_detail(scope, case_id)
        if result is None:
            raise HTTPException(status_code=404, detail="封面风险案件不存在")
        return CoverRiskCaseDetailResponse.model_validate(result)

    @router.post("/imports/preview", response_model=CoverImportResponse)
    async def preview_import(
        file: UploadFile = File(...),
        import_kind: str = Form(default="channels"),
        sheet_name: str = Form(default=""),
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverImportResponse:
        if import_kind not in {"baseline", "channels", "videos"}:
            raise HTTPException(status_code=400, detail="不支持的导入类型")
        file_name = Path(file.filename or "cover-import.xlsx").name
        if Path(file_name).suffix.lower() != ".xlsx":
            raise HTTPException(status_code=400, detail="封面巡检当前仅支持 .xlsx 文件")
        upload_id = str(uuid4())
        target_dir = Path(import_root) / upload_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "source.xlsx"
        digest = sha256()
        total_bytes = 0
        try:
            with target_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max(int(import_max_bytes), 1):
                        raise HTTPException(status_code=413, detail="导入文件超过大小限制")
                    digest.update(chunk)
                    output.write(chunk)
            if total_bytes == 0:
                raise HTTPException(status_code=400, detail="导入文件为空")
            result = await run_in_threadpool(
                store.create_import_preview,
                access_scope(user),
                import_kind=import_kind,
                source_file_name=file_name,
                source_file_sha256=digest.hexdigest(),
                source_file_path=str(target_path),
                sheet_name=sheet_name,
            )
        except HTTPException:
            target_path.unlink(missing_ok=True)
            raise
        except (BadZipFile, InvalidFileException, ValueError, OSError) as exc:
            target_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if Path(result["source_file_path"]).resolve() != target_path.resolve():
            target_path.unlink(missing_ok=True)
        return CoverImportResponse.model_validate(result)

    @router.get("/imports/{import_id}", response_model=CoverImportResponse)
    def get_import(
        import_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverImportResponse:
        result = store.get_import(access_scope(user), import_id)
        if result is None:
            raise HTTPException(status_code=404, detail="导入批次不存在")
        return CoverImportResponse.model_validate(result)

    @router.post("/imports/{import_id}/confirm", response_model=CoverImportResponse)
    async def confirm_import(
        import_id: str,
        user: dict[str, Any] = Depends(current_user_dependency),
    ) -> CoverImportResponse:
        try:
            result = await run_in_threadpool(store.confirm_import, access_scope(user), import_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="导入批次不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return CoverImportResponse.model_validate(result)

    return router
