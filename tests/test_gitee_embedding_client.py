from __future__ import annotations

import time
from http.client import RemoteDisconnected

import pytest

from scripts import gitee_embedding_client_v1 as gitee_client


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"data": []}'


def test_gitee_http_json_enforces_total_request_budget(monkeypatch) -> None:
    observed_timeouts: list[float] = []

    def fake_urlopen(_request, *, timeout: float):
        observed_timeouts.append(float(timeout))
        time.sleep(float(timeout))
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(gitee_client.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gitee_client.random, "uniform", lambda *_args: 0.0)
    started_at = time.monotonic()
    with pytest.raises(gitee_client.GiteeEmbeddingError, match="simulated timeout"):
        gitee_client.gitee_http_json(
            url="https://example.invalid/embeddings",
            token="test-token",
            payload={"input": ["test"]},
            timeout=0.05,
            max_attempts=4,
            retry_backoff_seconds=0,
            total_timeout_seconds=0.12,
        )
    elapsed = time.monotonic() - started_at

    assert 2 <= len(observed_timeouts) <= 4
    assert elapsed < 0.3
    assert sum(observed_timeouts) <= 0.15


def test_gitee_embed_texts_forwards_retry_budget(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_http_json(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"data": [{"embedding": [0.1, 0.2]}]}

    monkeypatch.setattr(gitee_client, "gitee_http_json", fake_http_json)
    embeddings = gitee_client.gitee_embed_texts(
        endpoint="https://example.invalid/embeddings",
        token="test-token",
        model="test-model",
        texts=["test"],
        dimensions=2,
        timeout=8,
        max_attempts=2,
        total_timeout_seconds=15,
    )

    assert embeddings == [[0.1, 0.2]]
    assert captured["timeout"] == 8
    assert captured["max_attempts"] == 2
    assert captured["total_timeout_seconds"] == 15


def test_gitee_http_json_retries_remote_disconnect(monkeypatch) -> None:
    attempts = 0
    delays: list[float] = []

    def fake_urlopen(_request, *, timeout: float):
        nonlocal attempts
        attempts += 1
        assert timeout == 1.0
        if attempts < 3:
            raise RemoteDisconnected("simulated disconnect")
        return _FakeResponse()

    monkeypatch.setattr(gitee_client.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gitee_client.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(gitee_client.time, "sleep", delays.append)

    response = gitee_client.gitee_http_json(
        url="https://example.invalid/embeddings",
        token="test-token",
        payload={"input": ["test"]},
        timeout=1,
        max_attempts=4,
        retry_backoff_seconds=0.25,
    )

    assert response == {"data": []}
    assert attempts == 3
    assert delays == [0.25, 0.5]
