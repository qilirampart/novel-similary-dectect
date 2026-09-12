from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
import math
import mimetypes
from pathlib import Path
import re
import socket
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from service.cover_monitor.prompts import (
    PROMPT_VERSION,
    build_cover_prompt,
    build_cover_risk_verification_prompt,
    cover_prompt_hash,
)


RISK_VALUES = {"safe", "review", "risk", "unknown"}
RISK_TAGS = {"未成年人", "孕妇", "色情或性暗示", "学生或校园"}


@dataclass(frozen=True)
class CoverVisionProfile:
    api_base: str
    api_key: str
    model: str
    timeout_seconds: float = 90

    def validate(self) -> None:
        if not self.api_base.strip() or not self.api_key.strip() or not self.model.strip():
            raise ValueError("封面视觉模型配置不完整")


@dataclass(frozen=True)
class CoverDetectionResult:
    overall_risk: str
    risk_tags: tuple[str, ...]
    summary: str
    evidence: str
    confidence: float
    provider: str
    model: str
    prompt_version: str
    prompt_hash: str
    intensity: str
    raw_response: str
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoverReviewOutcome:
    status: str
    result: Optional[CoverDetectionResult] = None
    error_type: str = ""
    error_message: str = ""
    raw_response: str = ""
    provider_status: Optional[int] = None
    duration_seconds: float = 0


class CoverVisionReviewer:
    def __init__(
        self,
        profile: CoverVisionProfile,
        *,
        max_image_bytes: int = 12 * 1024 * 1024,
        opener: Any = None,
    ) -> None:
        profile.validate()
        self.profile = profile
        self.max_image_bytes = max(int(max_image_bytes), 1024)
        # Deliberately ignore process-wide proxy variables. The cover proxy is only for YouTube traffic.
        self._opener = opener or build_opener(ProxyHandler({}))

    def review(
        self,
        *,
        image_path: str | Path,
        video_title: str,
        intensity: str = "standard",
    ) -> CoverReviewOutcome:
        started = time.perf_counter()
        raw_content = ""
        response_status: Optional[int] = None
        try:
            prompt = build_cover_prompt(intensity)
            path = Path(image_path)
            if not path.is_file():
                raise FileNotFoundError("封面文件不存在")
            image_bytes = path.read_bytes()
            if not image_bytes:
                raise ValueError("封面文件为空")
            if len(image_bytes) > self.max_image_bytes:
                raise ValueError("封面文件超过视觉请求字节上限")
            payload = self._request_payload(
                image_bytes=image_bytes,
                mime_type=mimetypes.guess_type(path.name)[0] or "image/jpeg",
                prompt=prompt,
            )
            body, status = self._post_json(payload)
            response_status = status
            raw_content = self._extract_content(body)
            normalized = self._validate_model_result(self._parse_json_object(raw_content))
            if normalized["overall_risk"] == "risk":
                normalized, raw_content, response_status = self._confirm_risk(
                    image_bytes=image_bytes,
                    mime_type=mimetypes.guess_type(path.name)[0] or "image/jpeg",
                    primary=normalized,
                    primary_raw=raw_content,
                    primary_status=status,
                )
            duration = time.perf_counter() - started
            return CoverReviewOutcome(
                status="succeeded",
                provider_status=status,
                duration_seconds=duration,
                result=CoverDetectionResult(
                    **normalized,
                    provider=self._provider_name(),
                    model=self.profile.model,
                    prompt_version=PROMPT_VERSION,
                    prompt_hash=cover_prompt_hash(intensity),
                    intensity=intensity,
                    raw_response=raw_content,
                    duration_seconds=duration,
                ),
            )
        except Exception as exc:
            duration = time.perf_counter() - started
            error_type, provider_status = self._classify_error(exc)
            return CoverReviewOutcome(
                status="failed",
                error_type=error_type,
                error_message=str(exc)[:1000],
                raw_response=raw_content[:8000],
                provider_status=provider_status or response_status,
                duration_seconds=duration,
            )

    def _request_payload(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> dict[str, Any]:
        image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        return {
            "model": self.profile.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请仅根据图片中的可见证据检测这张视频封面。"},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
        }

    def _confirm_risk(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        primary: dict[str, Any],
        primary_raw: str,
        primary_status: int,
    ) -> tuple[dict[str, Any], str, int]:
        try:
            payload = self._request_payload(
                image_bytes=image_bytes,
                mime_type=mime_type,
                prompt=build_cover_risk_verification_prompt(),
            )
            body, status = self._post_json(payload)
            verification_raw = self._extract_content(body)
            verification = self._validate_risk_verification(
                self._parse_json_object(verification_raw),
                primary_tags=set(primary["risk_tags"]),
            )
            combined_raw = json.dumps(
                {"primary": primary_raw, "verification": verification_raw},
                ensure_ascii=False,
            )
            if verification["decision"] == "confirm":
                confirmed = dict(primary)
                confirmed["risk_tags"] = verification["confirmed_risk_tags"]
                confirmed["confidence"] = min(primary["confidence"], verification["confidence"])
                return confirmed, combined_raw, status
            return self._demote_unconfirmed_risk(primary, verification["reason"]), combined_raw, status
        except Exception as exc:
            reason = f"二次核验不可用（{type(exc).__name__}），风险证据需人工复核"
            return self._demote_unconfirmed_risk(primary, reason), primary_raw, primary_status

    @staticmethod
    def _demote_unconfirmed_risk(primary: dict[str, Any], reason: str) -> dict[str, Any]:
        demoted = dict(primary)
        demoted["overall_risk"] = "review"
        demoted["summary"] = "风险证据尚未通过二次确认，建议人工复核"
        demoted["evidence"] = f"二次核验未确认：{str(reason).strip()[:800]}"
        return demoted

    @staticmethod
    def _validate_risk_verification(
        payload: dict[str, Any],
        *,
        primary_tags: set[str],
    ) -> dict[str, Any]:
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"confirm", "review"}:
            raise ValueError("二次核验 decision 非法")
        raw_tags = payload.get("confirmed_risk_tags")
        if not isinstance(raw_tags, list):
            raise ValueError("二次核验 confirmed_risk_tags 必须是数组")
        tags = tuple(dict.fromkeys(str(tag).strip() for tag in raw_tags if str(tag).strip()))
        if set(tags) - RISK_TAGS or set(tags) - primary_tags:
            raise ValueError("二次核验包含非法或新增风险标签")
        if decision == "confirm" and not tags:
            raise ValueError("二次核验确认风险时必须包含标签")
        if decision == "confirm" and set(tags) != primary_tags:
            raise ValueError("二次核验未完整确认首轮风险标签")
        if decision == "review" and tags:
            raise ValueError("二次核验待复核时标签必须为空")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("二次核验 reason 不能为空")
        confidence = float(payload.get("confidence"))
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("二次核验 confidence 非法")
        return {
            "decision": decision,
            "confirmed_risk_tags": tags,
            "reason": reason,
            "confidence": confidence,
        }

    def _post_json(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        request = Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.profile.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self._opener.open(request, timeout=max(float(self.profile.timeout_seconds), 1)) as response:
            raw = response.read()
            status = int(getattr(response, "status", 200))
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("模型接口响应不是 JSON 对象")
        return decoded, status

    def _endpoint(self) -> str:
        base = self.profile.api_base.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"

    def _provider_name(self) -> str:
        match = re.match(r"^https?://([^/]+)", self.profile.api_base.strip(), flags=re.IGNORECASE)
        return match.group(1).lower() if match else "openai-compatible"

    @staticmethod
    def _extract_content(body: dict[str, Any]) -> str:
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        text = str(content or "").strip()
        if not text:
            raise ValueError("模型返回内容为空")
        return text

    @staticmethod
    def _parse_json_object(raw_text: str) -> dict[str, Any]:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型未返回 JSON 检测结果")
        payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("模型检测结果不是 JSON 对象")
        return payload

    @staticmethod
    def _validate_model_result(payload: dict[str, Any]) -> dict[str, Any]:
        required = {"overall_risk", "risk_tags", "summary", "evidence", "confidence"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError("模型结果缺少字段: " + ", ".join(missing))
        overall = str(payload["overall_risk"]).strip().lower()
        if overall not in RISK_VALUES:
            raise ValueError(f"非法 overall_risk: {overall}")
        if not isinstance(payload["risk_tags"], list):
            raise ValueError("risk_tags 必须是数组")
        tags = tuple(dict.fromkeys(str(tag).strip() for tag in payload["risk_tags"] if str(tag).strip()))
        unknown_tags = sorted(set(tags) - RISK_TAGS)
        if unknown_tags:
            raise ValueError("包含未定义风险标签: " + ", ".join(unknown_tags))
        if overall == "safe" and tags:
            raise ValueError("safe 结果的 risk_tags 必须为空")
        if overall == "risk" and not tags:
            raise ValueError("risk 结果必须至少包含一个风险标签")
        summary = str(payload["summary"] or "").strip()
        evidence = str(payload["evidence"] or "").strip()
        if not summary:
            raise ValueError("summary 必须为 1 至 80 个字符")
        summary = summary[:80]
        if not evidence:
            raise ValueError("evidence 不能为空")
        confidence = float(payload["confidence"])
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence 必须是 0 到 1 的有限数值")
        return {
            "overall_risk": overall,
            "risk_tags": tags,
            "summary": summary,
            "evidence": evidence,
            "confidence": confidence,
        }

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, Optional[int]]:
        if isinstance(exc, HTTPError):
            if exc.code in {401, 403}:
                return "auth", exc.code
            if exc.code == 402:
                return "quota", exc.code
            if exc.code == 429:
                return "rate_limit", exc.code
            return "provider_http", exc.code
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout", None
        if isinstance(exc, URLError):
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return "timeout", None
            return "network", None
        if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
            return "invalid_response", None
        if isinstance(exc, OSError):
            return "local_io", None
        return "unexpected", None
