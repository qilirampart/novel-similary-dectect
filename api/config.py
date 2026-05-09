from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Optional

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
    business_db_path: str = os.environ.get("NOVEL_SIMILARITY_BUSINESS_DB", "data/novel_similarity_web_v1.sqlite3")
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
    candidate_display_score_threshold: float = float(os.environ.get("NOVEL_CANDIDATE_DISPLAY_SCORE_THRESHOLD", "0.10"))
    cors_allowed_origins: str = os.environ.get(
        "NOVEL_SIMILARITY_CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:4175,http://localhost:4175",
    )
    # Keep batch tasks consumable even when the API is started directly without the helper script.
    auto_worker_enabled: bool = _env_bool("NOVEL_SIMILARITY_AUTOSTART_WORKER", "1")
    auto_worker_poll_seconds: float = float(os.environ.get("NOVEL_SIMILARITY_AUTOWORKER_POLL_SECONDS", "2.0"))

    def ensure_runtime_dirs(self) -> None:
        Path(self.task_upload_root).mkdir(parents=True, exist_ok=True)
        Path(self.task_export_root).mkdir(parents=True, exist_ok=True)

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
        )


SETTINGS = ApiSettings()
