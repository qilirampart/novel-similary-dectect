from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, request


DEFAULT_GITEE_API_ENDPOINT = "https://ai.gitee.com/v1/embeddings"
DEFAULT_GITEE_TOKEN_ENV = "GITEE_AI_TOKEN"


class GiteeEmbeddingError(RuntimeError):
    pass


def resolve_gitee_token(token: str, token_env: str = DEFAULT_GITEE_TOKEN_ENV) -> str:
    value = token or os.environ.get(token_env, "")
    if not value:
        raise GiteeEmbeddingError(
            f"Missing Gitee token. Set env {token_env} or pass a direct token."
        )
    return value


def gitee_http_json(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout: int = 300,
) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GiteeEmbeddingError(f"HTTP {exc.code} for {url}: {body}") from exc
    except error.URLError as exc:
        raise GiteeEmbeddingError(f"Request failed for {url}: {exc}") from exc


def probe_gitee_embedding_dimension(
    endpoint: str,
    token: str,
    model: str,
    dimensions: int,
    timeout: int = 120,
) -> int:
    resp = gitee_http_json(
        url=endpoint,
        token=token,
        payload={
            "model": model,
            "input": "dimension probe for Chinese novel embedding",
            "encoding_format": "float",
            "dimensions": dimensions,
        },
        timeout=timeout,
    )
    data = resp.get("data")
    if not isinstance(data, list) or not data:
        raise GiteeEmbeddingError(f"Unexpected Gitee response: {resp}")
    embedding = data[0].get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise GiteeEmbeddingError(f"Unexpected Gitee embedding payload: {resp}")
    return len(embedding)


def gitee_embed_texts(
    endpoint: str,
    token: str,
    model: str,
    texts: list[str],
    dimensions: int,
    timeout: int = 300,
) -> list[list[float]]:
    if not texts:
        return []
    resp = gitee_http_json(
        url=endpoint,
        token=token,
        payload={
            "model": model,
            "input": texts,
            "encoding_format": "float",
            "dimensions": dimensions,
        },
        timeout=timeout,
    )
    data = resp.get("data")
    if not isinstance(data, list):
        raise GiteeEmbeddingError(f"Unexpected Gitee response: {resp}")
    if len(data) != len(texts):
        raise GiteeEmbeddingError(
            f"Embedding count mismatch: expected {len(texts)}, got {len(data)}"
        )
    embeddings: list[list[float]] = []
    for item in data:
        vector = item.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise GiteeEmbeddingError(f"Unexpected Gitee embedding payload: {resp}")
        embeddings.append(vector)
    return embeddings
