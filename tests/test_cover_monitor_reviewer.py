from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

from service.cover_monitor.prompts import PROMPT_VERSION, build_cover_prompt, cover_prompt_hash
from service.cover_monitor.reviewer import CoverVisionProfile, CoverVisionReviewer


class FakeResponse:
    def __init__(self, body: dict, status: int = 200) -> None:
        self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, response) -> None:
        self.response = response
        self.request = None

    def open(self, request, timeout: float):
        self.request = request
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _profile() -> CoverVisionProfile:
    return CoverVisionProfile(
        api_base="https://vision.example/v1",
        api_key="test-key",
        model="vision-model",
        timeout_seconds=12,
    )


def _image(path: Path) -> None:
    path.write_bytes(b"fake-image-for-transport-test")


def test_reviewer_accepts_strict_structured_response_and_freezes_prompt(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    content = {
        "overall_risk": "review",
        "risk_tags": ["学生或校园"],
        "summary": "存在校园元素，建议复核",
        "evidence": "画面中可见校服和教室黑板",
        "confidence": 0.82,
    }
    opener = FakeOpener(FakeResponse({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}))

    outcome = CoverVisionReviewer(_profile(), opener=opener).review(
        image_path=image,
        video_title="测试视频",
        intensity="standard",
    )

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.overall_risk == "review"
    assert outcome.result.prompt_version == PROMPT_VERSION
    assert outcome.result.prompt_hash == cover_prompt_hash("standard")
    request_payload = json.loads(opener.request.data.decode("utf-8"))
    assert request_payload["messages"][0]["content"] == build_cover_prompt("standard")
    assert request_payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_reviewer_does_not_silently_coerce_invalid_safe_result(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    invalid = {
        "overall_risk": "safe",
        "risk_tags": ["未成年人"],
        "summary": "安全",
        "evidence": "未发现明确证据",
        "confidence": 0.9,
    }
    opener = FakeOpener(FakeResponse({"choices": [{"message": {"content": json.dumps(invalid, ensure_ascii=False)}}]}))

    outcome = CoverVisionReviewer(_profile(), opener=opener).review(image_path=image, video_title="测试")

    assert outcome.status == "failed"
    assert outcome.error_type == "invalid_response"
    assert outcome.provider_status == 200
    assert '"overall_risk": "safe"' in outcome.raw_response


def test_reviewer_truncates_overlong_summary_without_discarding_valid_detection(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    valid = {
        "overall_risk": "review",
        "risk_tags": ["学生或校园"],
        "summary": "校" * 95,
        "evidence": "画面中可见校服和教室黑板",
        "confidence": 0.82,
    }
    opener = FakeOpener(FakeResponse({"choices": [{"message": {"content": json.dumps(valid, ensure_ascii=False)}}]}))

    outcome = CoverVisionReviewer(_profile(), opener=opener).review(image_path=image, video_title="测试")

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.summary == "校" * 80


def test_reviewer_keeps_valid_unknown_distinct_from_request_failure(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    unknown = {
        "overall_risk": "unknown",
        "risk_tags": [],
        "summary": "图片信息不足",
        "evidence": "人物区域严重模糊",
        "confidence": 0.2,
    }
    opener = FakeOpener(FakeResponse({"choices": [{"message": {"content": json.dumps(unknown, ensure_ascii=False)}}]}))
    outcome = CoverVisionReviewer(_profile(), opener=opener).review(image_path=image, video_title="测试")

    assert outcome.status == "succeeded"
    assert outcome.result is not None and outcome.result.overall_risk == "unknown"
    assert outcome.error_type == ""


def test_reviewer_classifies_rate_limit(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    error = HTTPError("https://vision.example/v1/chat/completions", 429, "rate limit", {}, None)
    outcome = CoverVisionReviewer(_profile(), opener=FakeOpener(error)).review(
        image_path=image,
        video_title="测试",
    )

    assert outcome.status == "failed"
    assert outcome.error_type == "rate_limit"
    assert outcome.provider_status == 429


def test_reviewer_rejects_oversized_local_image_before_transport(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"x" * 2048)
    opener = FakeOpener(FakeResponse({}))
    outcome = CoverVisionReviewer(_profile(), max_image_bytes=1024, opener=opener).review(
        image_path=image,
        video_title="测试",
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "invalid_response"
    assert opener.request is None
