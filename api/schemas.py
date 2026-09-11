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


class DramaSubtitleCompareRequest(BaseModel):
    query_text: str = Field(..., min_length=1, description="Subtitle text to compare against drama subtitles.")
    top_k: Optional[int] = Field(default=None, ge=1, le=20)
    window_limit: Optional[int] = Field(default=None, ge=1, le=500)
    language_code: Optional[str] = Field(default=None, description="Optional corpus language override: zh, en, ko or ja. Portuguese can be auto-detected.")
    semantic_enabled: Optional[bool] = Field(default=None, description="Enable same-language semantic recall.")
    semantic_window_limit: Optional[int] = Field(default=None, ge=1, le=500)
    translation_fallback: Optional[bool] = Field(default=None, description="Translate only after a native-language no-match.")


class DramaSubtitleCompareResponse(BaseModel):
    request_id: str
    duration_seconds: float
    payload: dict[str, Any]


class DramaSubtitleCue(BaseModel):
    start_seconds: float = Field(..., ge=0, description="Cue start time in seconds.")
    end_seconds: float = Field(..., ge=0, description="Cue end time in seconds.")
    text: str = Field(..., min_length=1, description="Original cue text.")


class DramaSubtitleVideoCompareRequest(BaseModel):
    query_text: str = Field(..., min_length=1, description="Complete subtitle text for the video-level fast screen.")
    cues: list[DramaSubtitleCue] = Field(..., min_length=1, description="Cue timeline used for server-side fallback segmentation.")
    top_k: Optional[int] = Field(default=None, ge=1, le=20)
    window_limit: Optional[int] = Field(default=None, ge=1, le=500)
    language_code: Optional[str] = Field(default=None, description="Optional corpus language override: zh, en, ko or ja. Portuguese can be auto-detected.")
    semantic_enabled: Optional[bool] = Field(default=None, description="Enable same-language semantic recall.")
    semantic_window_limit: Optional[int] = Field(default=None, ge=1, le=500)
    translation_fallback: Optional[bool] = Field(default=None, description="Translate only after a native-language no-match.")


class DramaSubtitleVideoCompareResponse(BaseModel):
    request_id: str
    duration_seconds: float
    payload: dict[str, Any]


class DramaSubtitleTaskCreateResponse(BaseModel):
    task_id: str
    status: str
    source_file_name: str


class DramaSubtitleTaskListResponse(BaseModel):
    items: list[dict[str, Any]]
    limit: int
    offset: int


class DramaSubtitleTaskDetailResponse(BaseModel):
    task: dict[str, Any]
    items: list[dict[str, Any]]


class TaskCreateResponse(BaseModel):
    task_id: str
    status: str
    detection_mode: str
    source_file_name: str


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Login username.")
    password: str = Field(..., min_length=1, description="Login password.")


class UserProfileResponse(BaseModel):
    user: dict[str, Any]


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Unique username.")
    password: str = Field(..., min_length=1, description="Initial password.")
    display_name: str = Field(..., min_length=1, description="Display name.")
    role: str = Field(default="operator", description="User role.")


class UserListResponse(BaseModel):
    items: list[dict[str, Any]]
    current_user_id: int


class UpdateUserRequest(BaseModel):
    display_name: str = Field(..., min_length=1, description="Display name.")
    role: Optional[str] = Field(default=None, description="Optional role update.")
    is_active: Optional[bool] = Field(default=None, description="Optional active flag update.")


class TaskListResponse(BaseModel):
    items: list[dict[str, Any]]
    limit: int
    offset: int


class TaskDetailResponse(BaseModel):
    task: dict[str, Any]
    items: list[dict[str, Any]]
    item_limit: int
    item_offset: int
    item_total: int
    result_stats: dict[str, int]


class BasicTaskResponse(BaseModel):
    task: dict[str, Any]


class TaskResultResponse(BaseModel):
    result: dict[str, Any]


class ResultListResponse(BaseModel):
    items: list[dict[str, Any]]
    limit: int
    offset: int
    total: Optional[int] = None
    stats: Optional[dict[str, int]] = None


class BulkActionResponse(BaseModel):
    status: str
    affected_count: int
    message: str


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


class ReadinessResponse(BaseModel):
    status: str
    service: str
    version: str
    checks: dict[str, str]
