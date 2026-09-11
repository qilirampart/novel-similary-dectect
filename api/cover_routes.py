from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zipfile import BadZipFile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel

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
    import_max_bytes: int = 150 * 1024 * 1024,
    current_user_dependency: Callable[..., dict[str, Any]],
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
