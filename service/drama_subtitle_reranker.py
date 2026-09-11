from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.gitee_embedding_client_v1 import (
    DEFAULT_GITEE_TOKEN_ENV,
    GiteeEmbeddingError,
    gitee_http_json,
    resolve_gitee_token,
)


DEFAULT_SENTENCE_SIMILARITY_ENDPOINT = "https://ai.gitee.com/v1/sentence-similarity"
DEFAULT_RERANKER_MODEL = "bce-reranker-base_v1"


class DramaSubtitleRerankerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DramaSubtitleRerankerConfig:
    endpoint: str = DEFAULT_SENTENCE_SIMILARITY_ENDPOINT
    model: str = DEFAULT_RERANKER_MODEL
    gitee_token: str = ""
    gitee_token_env: str = DEFAULT_GITEE_TOKEN_ENV
    timeout_seconds: int = 120


def rerank_drama_subtitle_candidates(
    *,
    query_text: str,
    candidates: list[dict[str, Any]],
    config: DramaSubtitleRerankerConfig = DramaSubtitleRerankerConfig(),
) -> list[dict[str, Any]]:
    normalized_query = str(query_text or "").strip()
    if not normalized_query:
        raise ValueError("query_text is empty")
    if not candidates:
        return []
    if config.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")

    texts = [
        str(
            (candidate.get("evidence") or {}).get("window_text")
            or (candidate.get("evidence") or {}).get("window_text_preview")
            or ""
        )
        for candidate in candidates
    ]
    try:
        token = resolve_gitee_token(config.gitee_token, config.gitee_token_env)
        response = gitee_http_json(
            url=config.endpoint,
            token=token,
            payload={
                "model": config.model,
                "inputs": {
                    "source_sentence": normalized_query,
                    "sentences": texts,
                },
                "normalize": True,
            },
            timeout=config.timeout_seconds,
        )
    except GiteeEmbeddingError as exc:
        raise DramaSubtitleRerankerError(str(exc)) from exc

    if not isinstance(response, list) or len(response) != len(candidates):
        raise DramaSubtitleRerankerError(
            f"unexpected reranker response: expected {len(candidates)} scores, got {response!r}"
        )
    reranked: list[dict[str, Any]] = []
    for candidate, score in zip(candidates, response):
        try:
            reranker_score = float(score)
        except (TypeError, ValueError) as exc:
            raise DramaSubtitleRerankerError(f"invalid reranker score: {score!r}") from exc
        enriched = dict(candidate)
        enriched["reranker_score"] = round(reranker_score, 6)
        enriched["reranker_model"] = config.model
        reranked.append(enriched)
    reranked.sort(
        key=lambda item: (
            -float(item["reranker_score"]),
            int(item.get("rank") or 10**6),
            str(item.get("book_id") or ""),
            int(item.get("episode_order") or 0),
        )
    )
    for rank, candidate in enumerate(reranked, start=1):
        candidate["reranker_rank"] = rank
    return reranked
