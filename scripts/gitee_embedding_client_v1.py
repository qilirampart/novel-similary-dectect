from __future__ import annotations

import json
import os
import random
import time
from http import client as http_client
from typing import Any
from urllib import error, request


DEFAULT_GITEE_API_ENDPOINT = "https://ai.gitee.com/v1/embeddings"
DEFAULT_GITEE_TOKEN_ENV = "GITEE_AI_TOKEN"
RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


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
    max_attempts: int = 4,
    retry_backoff_seconds: float = 2.0,
    total_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    if max_attempts <= 0:
        raise ValueError("max_attempts must be > 0")
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    last_error: GiteeEmbeddingError | None = None
    deadline = (
        time.monotonic() + max(float(total_timeout_seconds), 0.001)
        if total_timeout_seconds is not None
        else None
    )
    for attempt in range(1, max_attempts + 1):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            break
        attempt_timeout = max(
            min(float(timeout), remaining) if remaining is not None else float(timeout),
            0.001,
        )
        try:
            with request.urlopen(req, timeout=attempt_timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = GiteeEmbeddingError(f"HTTP {exc.code} for {url}: {body}")
            retryable = exc.code in RETRYABLE_HTTP_STATUS_CODES
        except (error.URLError, TimeoutError, ConnectionError, http_client.HTTPException) as exc:
            last_error = GiteeEmbeddingError(f"Request failed for {url}: {exc}")
            retryable = True

        if not retryable or attempt == max_attempts:
            raise last_error

        # Add a small jitter so concurrent batches do not retry in lockstep.
        delay = retry_backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        if deadline is not None:
            delay = min(delay, max(deadline - time.monotonic(), 0.0))
        time.sleep(delay)

    if last_error is not None:
        raise last_error
    raise GiteeEmbeddingError(f"Request budget exhausted for {url}")


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
    max_attempts: int = 4,
    total_timeout_seconds: float | None = None,
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
        max_attempts=max_attempts,
        total_timeout_seconds=total_timeout_seconds,
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
