from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Optional

from service.drama_subtitle_semantic_retrieval import DramaSubtitleSemanticConfig
from service.semantic_retrieval import SemanticRetrievalConfig


ROOT_DIR = Path(__file__).resolve().parent.parent
LOCAL_ENV_PS1 = ROOT_DIR / ".env.local.ps1"


def _load_local_ps1_env(path: Path) -> None:
    if not path.exists():
        return
    pattern = re.compile(
        r"^\s*\$env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.+?)\s*$"
    )
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        name = match.group("name")
        value = match.group("value").strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        os.environ.setdefault(name, value)


_load_local_ps1_env(LOCAL_ENV_PS1)


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiSettings:
    db_path: str = os.environ.get("NOVEL_SIMILARITY_DB", "data/novel_similarity_v2.sqlite3")
    drama_subtitle_db_path: str = os.environ.get(
        "DRAMA_SUBTITLE_SIMILARITY_DB",
        "data/drama_subtitle_similarity_v1.sqlite3",
    )
    business_db_path: str = os.environ.get("NOVEL_SIMILARITY_BUSINESS_DB", "data/novel_similarity_web_v1.sqlite3")
    cover_monitor_db_path: str = os.environ.get(
        "COVER_MONITOR_DB", "data/cover_monitor_v1.sqlite3"
    )
    cover_monitor_asset_root: str = os.environ.get(
        "COVER_MONITOR_ASSET_ROOT", "runtime/cover_monitor/assets"
    )
    cover_monitor_import_root: str = os.environ.get(
        "COVER_MONITOR_IMPORT_ROOT", "runtime/cover_monitor/imports"
    )
    cover_monitor_import_max_bytes: int = int(
        os.environ.get("COVER_MONITOR_IMPORT_MAX_BYTES", str(150 * 1024 * 1024))
    )
    cover_monitor_proxy_url: str = os.environ.get("COVER_MONITOR_PROXY_URL", "")
    cover_monitor_cookie_path: str = os.environ.get("COVER_MONITOR_COOKIE_PATH", "")
    cover_monitor_network_timeout_seconds: float = float(
        os.environ.get("COVER_MONITOR_NETWORK_TIMEOUT_SECONDS", "30")
    )
    cover_monitor_download_max_bytes: int = int(
        os.environ.get("COVER_MONITOR_DOWNLOAD_MAX_BYTES", str(12 * 1024 * 1024))
    )
    cover_monitor_image_max_pixels: int = int(
        os.environ.get("COVER_MONITOR_IMAGE_MAX_PIXELS", "40000000")
    )
    cover_monitor_vision_api_base: str = os.environ.get("COVER_MONITOR_VISION_API_BASE", "")
    cover_monitor_vision_api_key: str = os.environ.get("COVER_MONITOR_VISION_API_KEY", "")
    cover_monitor_vision_model: str = os.environ.get("COVER_MONITOR_VISION_MODEL", "")
    cover_monitor_vision_timeout_seconds: float = float(
        os.environ.get("COVER_MONITOR_VISION_TIMEOUT_SECONDS", "90")
    )
    task_upload_root: str = os.environ.get("NOVEL_SIMILARITY_TASK_UPLOAD_ROOT", "runtime/task_uploads")
    task_export_root: str = os.environ.get("NOVEL_SIMILARITY_TASK_EXPORT_ROOT", "runtime/task_exports")
    task_created_by_default: str = os.environ.get("NOVEL_SIMILARITY_TASK_CREATED_BY", "api")
    task_worker_name: str = os.environ.get("NOVEL_SIMILARITY_TASK_WORKER_NAME", "local-worker")
    semantic_backend: str = os.environ.get("NOVEL_SEMANTIC_BACKEND", "remote")
    semantic_remote_embedding_backend: str = os.environ.get("NOVEL_SEMANTIC_REMOTE_BACKEND", "gitee_api")
    semantic_ollama_url: str = os.environ.get("NOVEL_SEMANTIC_OLLAMA_URL", "http://127.0.0.1:11434")
    semantic_qdrant_url: str = os.environ.get("NOVEL_SEMANTIC_QDRANT_URL", "http://127.0.0.1:6333")
    semantic_model: str = os.environ.get("NOVEL_SEMANTIC_MODEL", "Qwen3-Embedding-8B")
    semantic_gitee_endpoint: str = os.environ.get("NOVEL_SEMANTIC_GITEE_ENDPOINT", "https://ai.gitee.com/v1/embeddings")
    semantic_gitee_token: str = os.environ.get("NOVEL_SEMANTIC_GITEE_TOKEN", "")
    semantic_gitee_token_env: str = os.environ.get("NOVEL_SEMANTIC_GITEE_TOKEN_ENV", "GITEE_AI_TOKEN")
    semantic_gitee_dimensions: int = int(os.environ.get("NOVEL_SEMANTIC_GITEE_DIMENSIONS", "1024"))
    semantic_chapter_collection: str = os.environ.get("NOVEL_SEMANTIC_CHAPTER_COLLECTION", "novel_chapter_embeddings")
    semantic_chunk_collection: str = os.environ.get(
        "NOVEL_SEMANTIC_CHUNK_COLLECTION",
        "novel_semantic_chunk_embeddings_qwen3_8b_1024_v1",
    )
    semantic_query_window_size: int = int(os.environ.get("NOVEL_SEMANTIC_QUERY_WINDOW_SIZE", "800"))
    semantic_query_overlap_size: int = int(os.environ.get("NOVEL_SEMANTIC_QUERY_OVERLAP_SIZE", "200"))
    semantic_chapter_top_k: int = int(os.environ.get("NOVEL_SEMANTIC_CHAPTER_TOP_K", "0"))
    semantic_chunk_top_k: int = int(os.environ.get("NOVEL_SEMANTIC_CHUNK_TOP_K", "12"))
    semantic_score_threshold: float = float(os.environ.get("NOVEL_SEMANTIC_SCORE_THRESHOLD", "0.0"))
    semantic_embed_timeout_seconds: int = int(os.environ.get("NOVEL_SEMANTIC_EMBED_TIMEOUT_SECONDS", "8"))
    semantic_embed_max_attempts: int = int(os.environ.get("NOVEL_SEMANTIC_EMBED_MAX_ATTEMPTS", "2"))
    semantic_embed_total_timeout_seconds: int = int(
        os.environ.get("NOVEL_SEMANTIC_EMBED_TOTAL_TIMEOUT_SECONDS", "15")
    )
    semantic_qdrant_timeout_seconds: int = int(os.environ.get("NOVEL_SEMANTIC_QDRANT_TIMEOUT_SECONDS", "8"))
    candidate_display_score_threshold: float = float(os.environ.get("NOVEL_CANDIDATE_DISPLAY_SCORE_THRESHOLD", "0.01"))
    cors_allowed_origins: str = os.environ.get(
        "NOVEL_SIMILARITY_CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:4175,http://localhost:4175",
    )
    # Keep batch tasks consumable even when the API is started directly without the helper script.
    auto_worker_enabled: bool = _env_bool("NOVEL_SIMILARITY_AUTOSTART_WORKER", "1")
    auto_worker_count: int = int(os.environ.get("NOVEL_SIMILARITY_AUTOWORKER_COUNT", "2"))
    auto_worker_poll_seconds: float = float(os.environ.get("NOVEL_SIMILARITY_AUTOWORKER_POLL_SECONDS", "2.0"))
    fault_diagnostics_enabled: bool = _env_bool("NOVEL_SIMILARITY_FAULT_DIAGNOSTICS_ENABLED", "1")
    fault_diagnostics_signal: str = os.environ.get("NOVEL_SIMILARITY_FAULT_DIAGNOSTICS_SIGNAL", "SIGUSR1")
    task_recovery_stale_seconds: float = float(
        os.environ.get("NOVEL_SIMILARITY_TASK_RECOVERY_STALE_SECONDS", "120.0")
    )
    task_recovery_sweep_seconds: float = float(
        os.environ.get("NOVEL_SIMILARITY_TASK_RECOVERY_SWEEP_SECONDS", "15.0")
    )
    task_item_parallelism: int = int(os.environ.get("NOVEL_SIMILARITY_TASK_ITEM_PARALLELISM", "2"))
    worker_shutdown_timeout_seconds: float = float(
        os.environ.get("NOVEL_SIMILARITY_WORKER_SHUTDOWN_TIMEOUT_SECONDS", "10.0")
    )
    task_queue_eta_seconds_per_item: float = float(
        os.environ.get("NOVEL_SIMILARITY_TASK_QUEUE_ETA_SECONDS_PER_ITEM", "5.0")
    )
    drama_subtitle_semantic_enabled: bool = _env_bool("DRAMA_SUBTITLE_SEMANTIC_ENABLED", "1")
    drama_subtitle_semantic_backend: str = os.environ.get("DRAMA_SUBTITLE_SEMANTIC_BACKEND", "gitee_api")
    drama_subtitle_semantic_ollama_url: str = os.environ.get(
        "DRAMA_SUBTITLE_SEMANTIC_OLLAMA_URL", "http://127.0.0.1:11434"
    )
    drama_subtitle_semantic_qdrant_url: str = os.environ.get(
        "DRAMA_SUBTITLE_SEMANTIC_QDRANT_URL", "http://127.0.0.1:6333"
    )
    drama_subtitle_semantic_collection: str = os.environ.get(
        "DRAMA_SUBTITLE_SEMANTIC_COLLECTION", "drama_subtitle_window_embeddings_qwen3_4b_2560_v1"
    )
    drama_subtitle_semantic_model: str = os.environ.get("DRAMA_SUBTITLE_SEMANTIC_MODEL", "Qwen3-Embedding-4B")
    drama_subtitle_semantic_gitee_endpoint: str = os.environ.get(
        "DRAMA_SUBTITLE_SEMANTIC_GITEE_ENDPOINT", "https://ai.gitee.com/v1/embeddings"
    )
    drama_subtitle_semantic_gitee_token: str = os.environ.get("DRAMA_SUBTITLE_SEMANTIC_GITEE_TOKEN", "")
    drama_subtitle_semantic_gitee_token_env: str = os.environ.get(
        "DRAMA_SUBTITLE_SEMANTIC_GITEE_TOKEN_ENV", "GITEE_AI_TOKEN"
    )
    drama_subtitle_semantic_gitee_dimensions: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_GITEE_DIMENSIONS", "2560")
    )
    drama_subtitle_semantic_query_window_size: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_QUERY_WINDOW_SIZE", "800")
    )
    drama_subtitle_semantic_query_overlap_size: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_QUERY_OVERLAP_SIZE", "200")
    )
    drama_subtitle_semantic_embed_timeout_seconds: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_EMBED_TIMEOUT_SECONDS", "8")
    )
    drama_subtitle_semantic_embed_max_attempts: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_EMBED_MAX_ATTEMPTS", "2")
    )
    drama_subtitle_semantic_embed_total_timeout_seconds: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_EMBED_TOTAL_TIMEOUT_SECONDS", "15")
    )
    drama_subtitle_semantic_qdrant_timeout_seconds: int = int(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_QDRANT_TIMEOUT_SECONDS", "8")
    )
    drama_subtitle_semantic_qdrant_single_flight_enabled: bool = _env_bool(
        "DRAMA_SUBTITLE_SEMANTIC_QDRANT_SINGLE_FLIGHT_ENABLED", "1"
    )
    drama_subtitle_semantic_qdrant_queue_timeout_seconds: float = float(
        os.environ.get("DRAMA_SUBTITLE_SEMANTIC_QDRANT_QUEUE_TIMEOUT_SECONDS", "12")
    )
    drama_subtitle_semantic_qdrant_lock_path: str = os.environ.get(
        "DRAMA_SUBTITLE_SEMANTIC_QDRANT_LOCK_PATH",
        "/tmp/novel-similarity-drama-qdrant.lock",
    )
    drama_subtitle_max_episode_order: int = int(
        os.environ.get("DRAMA_SUBTITLE_MAX_EPISODE_ORDER", "0")
    )
    drama_subtitle_translation_enabled: bool = _env_bool("DRAMA_SUBTITLE_TRANSLATION_ENABLED", "1")
    drama_subtitle_translation_provider_order: str = os.environ.get(
        "DRAMA_SUBTITLE_TRANSLATION_PROVIDER_ORDER", "tencent,deepl"
    )
    drama_subtitle_deepl_api_key: str = os.environ.get("DRAMA_SUBTITLE_DEEPL_API_KEY", "")
    drama_subtitle_tencent_secret_id: str = os.environ.get(
        "DRAMA_SUBTITLE_TENCENT_SECRET_ID", os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    )
    drama_subtitle_tencent_secret_key: str = os.environ.get(
        "DRAMA_SUBTITLE_TENCENT_SECRET_KEY", os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    )
    drama_subtitle_tencent_region: str = os.environ.get(
        "DRAMA_SUBTITLE_TENCENT_REGION", os.environ.get("TENCENTCLOUD_REGION", "ap-shanghai")
    )
    drama_subtitle_tencent_project_id: int = int(
        os.environ.get("DRAMA_SUBTITLE_TENCENT_PROJECT_ID", os.environ.get("TENCENTCLOUD_PROJECT_ID", "0"))
    )
    drama_subtitle_tencent_endpoint: str = os.environ.get(
        "DRAMA_SUBTITLE_TENCENT_ENDPOINT", "https://tmt.tencentcloudapi.com"
    )
    drama_subtitle_translation_timeout_seconds: float = float(
        os.environ.get("DRAMA_SUBTITLE_TRANSLATION_TIMEOUT_SECONDS", "12")
    )
    drama_subtitle_translation_total_timeout_seconds: float = float(
        os.environ.get("DRAMA_SUBTITLE_TRANSLATION_TOTAL_TIMEOUT_SECONDS", "30")
    )
    drama_subtitle_translation_target_parallelism: int = int(
        os.environ.get("DRAMA_SUBTITLE_TRANSLATION_TARGET_PARALLELISM", "2")
    )
    drama_subtitle_translation_primary_target_head_start_seconds: float = float(
        os.environ.get("DRAMA_SUBTITLE_TRANSLATION_PRIMARY_TARGET_HEAD_START_SECONDS", "0")
    )
    drama_subtitle_translation_alignment_review_enabled: bool = _env_bool(
        "DRAMA_SUBTITLE_TRANSLATION_ALIGNMENT_REVIEW_ENABLED", "1"
    )

    def ensure_runtime_dirs(self) -> None:
        Path(self.task_upload_root).mkdir(parents=True, exist_ok=True)
        Path(self.task_export_root).mkdir(parents=True, exist_ok=True)
        Path(self.cover_monitor_asset_root).mkdir(parents=True, exist_ok=True)
        Path(self.cover_monitor_import_root).mkdir(parents=True, exist_ok=True)

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        items = [item.strip() for item in self.cors_allowed_origins.split(",")]
        return [item for item in items if item]

    def build_semantic_config(self, merged_top_k: Optional[int] = None) -> SemanticRetrievalConfig:
        gitee_token = self.semantic_gitee_token or os.environ.get(self.semantic_gitee_token_env, "")
        return SemanticRetrievalConfig(
            backend=self.semantic_backend,
            remote_embedding_backend=self.semantic_remote_embedding_backend,
            ollama_url=self.semantic_ollama_url,
            qdrant_url=self.semantic_qdrant_url,
            model=self.semantic_model,
            gitee_endpoint=self.semantic_gitee_endpoint,
            gitee_token=gitee_token,
            gitee_token_env=self.semantic_gitee_token_env,
            gitee_dimensions=self.semantic_gitee_dimensions,
            chapter_collection=self.semantic_chapter_collection,
            chunk_collection=self.semantic_chunk_collection,
            query_window_size=self.semantic_query_window_size,
            query_overlap_size=self.semantic_query_overlap_size,
            chapter_top_k=self.semantic_chapter_top_k,
            chunk_top_k=self.semantic_chunk_top_k,
            merged_top_k=merged_top_k or 20,
            score_threshold=self.semantic_score_threshold,
            embed_timeout_seconds=self.semantic_embed_timeout_seconds,
            embed_max_attempts=self.semantic_embed_max_attempts,
            embed_total_timeout_seconds=self.semantic_embed_total_timeout_seconds,
            qdrant_timeout_seconds=self.semantic_qdrant_timeout_seconds,
        )

    def build_drama_subtitle_semantic_config(self) -> DramaSubtitleSemanticConfig:
        gitee_token = self.drama_subtitle_semantic_gitee_token or os.environ.get(
            self.drama_subtitle_semantic_gitee_token_env, ""
        )
        return DramaSubtitleSemanticConfig(
            collection=self.drama_subtitle_semantic_collection,
            embedding_backend=self.drama_subtitle_semantic_backend,
            ollama_url=self.drama_subtitle_semantic_ollama_url,
            qdrant_url=self.drama_subtitle_semantic_qdrant_url,
            model=self.drama_subtitle_semantic_model,
            gitee_endpoint=self.drama_subtitle_semantic_gitee_endpoint,
            gitee_token=gitee_token,
            gitee_token_env=self.drama_subtitle_semantic_gitee_token_env,
            gitee_dimensions=self.drama_subtitle_semantic_gitee_dimensions,
            query_window_size=self.drama_subtitle_semantic_query_window_size,
            query_overlap_size=self.drama_subtitle_semantic_query_overlap_size,
            embed_timeout_seconds=self.drama_subtitle_semantic_embed_timeout_seconds,
            embed_max_attempts=self.drama_subtitle_semantic_embed_max_attempts,
            embed_total_timeout_seconds=self.drama_subtitle_semantic_embed_total_timeout_seconds,
            qdrant_timeout_seconds=self.drama_subtitle_semantic_qdrant_timeout_seconds,
            qdrant_single_flight_enabled=self.drama_subtitle_semantic_qdrant_single_flight_enabled,
            qdrant_queue_timeout_seconds=self.drama_subtitle_semantic_qdrant_queue_timeout_seconds,
            qdrant_lock_path=self.drama_subtitle_semantic_qdrant_lock_path,
            max_episode_order=self.drama_subtitle_max_episode_order,
        )

    def build_drama_subtitle_translation_config(self):
        from service.drama_subtitle_translation import DramaSubtitleTranslationConfig

        return DramaSubtitleTranslationConfig(
            enabled=self.drama_subtitle_translation_enabled,
            deepl_api_key=self.drama_subtitle_deepl_api_key,
            provider_order=tuple(
                item.strip().lower()
                for item in self.drama_subtitle_translation_provider_order.split(",")
                if item.strip()
            ),
            tencent_secret_id=self.drama_subtitle_tencent_secret_id,
            tencent_secret_key=self.drama_subtitle_tencent_secret_key,
            tencent_region=self.drama_subtitle_tencent_region,
            tencent_project_id=self.drama_subtitle_tencent_project_id,
            tencent_endpoint=self.drama_subtitle_tencent_endpoint,
            timeout_seconds=self.drama_subtitle_translation_timeout_seconds,
            total_timeout_seconds=self.drama_subtitle_translation_total_timeout_seconds,
            target_parallelism=self.drama_subtitle_translation_target_parallelism,
            primary_target_head_start_seconds=self.drama_subtitle_translation_primary_target_head_start_seconds,
            continuous_alignment_review_enabled=self.drama_subtitle_translation_alignment_review_enabled,
        )


SETTINGS = ApiSettings()
