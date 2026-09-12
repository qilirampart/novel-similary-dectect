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


class SequenceFakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout: float):
        self.requests.append(request)
        return self.responses.pop(0)


def _profile() -> CoverVisionProfile:
    return CoverVisionProfile(
        api_base="https://vision.example/v1",
        api_key="test-key",
        model="vision-model",
        timeout_seconds=12,
    )


def _image(path: Path) -> None:
    path.write_bytes(b"fake-image-for-transport-test")


def test_prompt_defines_visible_evidence_boundaries_for_common_drama_covers() -> None:
    prompt = build_cover_prompt("standard")

    assert "泼水、淋湿、跪地、争吵、拉扯或肢体冲突" in prompt
    assert "短裙、紧身、露肩或礼服" in prompt
    assert "不能单独作为色情或性暗示证据" in prompt
    assert "不能仅凭年轻外观、发型、身材或服装" in prompt
    assert "未成年人" in prompt
    assert "无法排除风险时使用 review，而不是 risk" in prompt


def test_prompt_requires_each_risk_tag_to_have_specific_visible_evidence() -> None:
    prompt = build_cover_prompt("strict")

    assert "每个 risk_tags 标签" in prompt
    assert "具体可见证据" in prompt
    assert "不得为了提高召回率强行映射" in prompt


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
    assert request_payload["messages"][1]["content"][0]["text"] == "请仅根据图片中的可见证据检测这张视频封面。"
    assert "测试视频" not in json.dumps(request_payload, ensure_ascii=False)
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


def test_reviewer_demotes_unconfirmed_risk_to_review(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    primary = {
        "overall_risk": "risk",
        "risk_tags": ["色情或性暗示"],
        "summary": "泼水场景存在性暗示",
        "evidence": "成年女性被泼水且衣物湿透",
        "confidence": 0.94,
    }
    verification = {
        "decision": "review",
        "confirmed_risk_tags": [],
        "reason": "仅见成人冲突和泼水，未见明确性行为或敏感部位裸露",
        "confidence": 0.91,
    }
    opener = SequenceFakeOpener([
        FakeResponse({"choices": [{"message": {"content": json.dumps(primary, ensure_ascii=False)}}]}),
        FakeResponse({"choices": [{"message": {"content": json.dumps(verification, ensure_ascii=False)}}]}),
    ])

    outcome = CoverVisionReviewer(_profile(), opener=opener).review(image_path=image, video_title="测试")

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.overall_risk == "review"
    assert outcome.result.risk_tags == ("色情或性暗示",)
    assert "二次核验未确认" in outcome.result.evidence
    assert len(opener.requests) == 2


def test_reviewer_keeps_risk_when_second_pass_confirms_visible_evidence(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    primary = {
        "overall_risk": "risk",
        "risk_tags": ["孕妇"],
        "summary": "可见明确孕妇",
        "evidence": "人物腹部呈清晰妊娠形态",
        "confidence": 0.96,
    }
    verification = {
        "decision": "confirm",
        "confirmed_risk_tags": ["孕妇"],
        "reason": "腹部轮廓和人物姿态均提供清晰、直接的妊娠证据",
        "confidence": 0.93,
    }
    opener = SequenceFakeOpener([
        FakeResponse({"choices": [{"message": {"content": json.dumps(primary, ensure_ascii=False)}}]}),
        FakeResponse({"choices": [{"message": {"content": json.dumps(verification, ensure_ascii=False)}}]}),
    ])

    outcome = CoverVisionReviewer(_profile(), opener=opener).review(image_path=image, video_title="测试")

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.overall_risk == "risk"
    assert outcome.result.risk_tags == ("孕妇",)
    assert len(opener.requests) == 2


def test_reviewer_demotes_risk_when_second_pass_only_confirms_part_of_primary_tags(tmp_path: Path) -> None:
    image = tmp_path / "cover.jpg"
    _image(image)
    primary = {
        "overall_risk": "risk",
        "risk_tags": ["孕妇", "色情或性暗示"],
        "summary": "疑似多项风险",
        "evidence": "人物腹部隆起且姿态暴露",
        "confidence": 0.95,
    }
    partial = {
        "decision": "confirm",
        "confirmed_risk_tags": ["孕妇"],
        "reason": "仅能确认孕妇证据",
        "confidence": 0.9,
    }
    opener = SequenceFakeOpener([
        FakeResponse({"choices": [{"message": {"content": json.dumps(primary, ensure_ascii=False)}}]}),
        FakeResponse({"choices": [{"message": {"content": json.dumps(partial, ensure_ascii=False)}}]}),
    ])

    outcome = CoverVisionReviewer(_profile(), opener=opener).review(image_path=image, video_title="测试")

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.overall_risk == "review"
    assert outcome.result.risk_tags == ("孕妇", "色情或性暗示")


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
