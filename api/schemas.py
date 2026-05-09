from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class CompareSingleRequest(BaseModel):
    query_text: str = Field(..., min_length=1, description="Source text or subtitle content to compare.")
    detection_mode: Literal["reuse", "rewrite"] = Field(..., description="Detection mode.")
    top_k: Optional[int] = Field(default=None, ge=1, le=20)
    compare_top_k: Optional[int] = Field(default=None, ge=1, le=100)
    merged_top_k: Optional[int] = Field(default=None, ge=1, le=200)
    candidate_display_score_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CompareSingleResponse(BaseModel):
    request_id: str
    duration_seconds: float
    payload: dict[str, Any]


class TaskCreateResponse(BaseModel):
    task_id: str
    status: str
    detection_mode: str
    source_file_name: str


class TaskListResponse(BaseModel):
    items: list[dict[str, Any]]
    limit: int
    offset: int


class TaskDetailResponse(BaseModel):
    task: dict[str, Any]
    items: list[dict[str, Any]]
    item_limit: int
    item_offset: int


class BasicTaskResponse(BaseModel):
    task: dict[str, Any]


class TaskResultResponse(BaseModel):
    result: dict[str, Any]


class ResultListResponse(BaseModel):
    items: list[dict[str, Any]]
    limit: int
    offset: int


class CompareReviewRequest(BaseModel):
    review_status: str = Field(..., min_length=1, description="Manual review status.")
    reviewer_name: str = Field(default="", description="Reviewer name.")
    review_note: str = Field(default="", description="Manual review note.")


class SystemStatusResponse(BaseModel):
    payload: dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
