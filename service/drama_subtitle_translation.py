from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from service.business_store import connect_business_db
from v2_common import now_ts


DEEPL_FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
TENCENT_TMT_ENDPOINT = "https://tmt.tencentcloudapi.com"
DEEPL_PROVIDER_KEY = "deepl-free-v2"
TENCENT_PROVIDER_KEY = "tencent-tmt-v1"
PROVIDER_KEY = DEEPL_PROVIDER_KEY
SUPPORTED_PROVIDERS = ("tencent", "deepl")
TARGETS_BY_SOURCE_LANGUAGE = {
    "zh": ("en", "ko", "ja"),
    "en": ("zh", "ko", "ja"),
    "ko": ("zh", "en", "ja"),
    "ja": ("zh", "ko", "en"),
    "pt": ("en", "zh", "ko", "ja"),
}
DEEPL_SOURCE_CODES = {"zh": "ZH", "en": "EN", "ko": "KO", "ja": "JA", "pt": "PT"}
DEEPL_TARGET_CODES = {"zh": "ZH", "en": "EN-US", "ko": "KO", "ja": "JA"}
TENCENT_LANGUAGE_CODES = {"zh": "zh", "en": "en", "ko": "ko", "ja": "ja", "pt": "pt"}
TRANSLATION_SEMANTIC_REVIEW_MIN_SCORE = 0.60
TRANSLATION_SEMANTIC_REVIEW_MIN_MARGIN = 0.05
TRANSLATION_SEMANTIC_QUERY_WINDOW_SIZE = 500
TRANSLATION_SEMANTIC_QUERY_OVERLAP_SIZE = 120
TRANSLATION_ALIGNMENT_MIN_SCORE = 0.40
TRANSLATION_ALIGNMENT_MIN_SEMANTIC_SCORE = 0.62
TRANSLATION_ALIGNMENT_MIN_EVIDENCE_CHARS = 60
TRANSLATION_ALIGNMENT_MIN_MATCHED_CHARS = 36
TRANSLATION_ALIGNMENT_MAX_LEGACY_SCORE = 0.25
TRANSLATION_ALIGNMENT_MIN_IMPROVEMENT = 0.15
# A long evidence window can dilute the ordered-alignment ratio even when a
# substantial, high-confidence dialogue block is present. This rescue path is
# deliberately narrower than the normal review gate and is not a global
# semantic-threshold change.
TRANSLATION_ALIGNMENT_RESCUE_MIN_SCORE = 0.35
TRANSLATION_ALIGNMENT_RESCUE_MIN_SEMANTIC_SCORE = 0.70
TRANSLATION_ALIGNMENT_RESCUE_MIN_EVIDENCE_CHARS = 100
TRANSLATION_ALIGNMENT_RESCUE_MIN_MATCHED_CHARS = 72
TRANSLATION_LEXICAL_PRECHECK_MIN_SHARED = 9
TRANSLATION_LEXICAL_PRECHECK_MIN_QUERY_COVERAGE = 0.015
TRANSLATION_LEXICAL_PRECHECK_MIN_EVIDENCE_COVERAGE = 0.08
TRANSLATION_LEXICAL_ALIGNMENT_MIN_SCORE = 0.45
TRANSLATION_LEXICAL_ALIGNMENT_MIN_EVIDENCE_CHARS = 100
TRANSLATION_LEXICAL_ALIGNMENT_MIN_MATCHED_CHARS = 72


class DramaSubtitleTranslationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DramaSubtitleTranslationConfig:
    enabled: bool = False
    deepl_api_key: str = ""
    endpoint: str = DEEPL_FREE_ENDPOINT
    provider_order: tuple[str, ...] = ("tencent", "deepl")
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_region: str = "ap-shanghai"
    tencent_project_id: int = 0
    tencent_endpoint: str = TENCENT_TMT_ENDPOINT
    timeout_seconds: float = 20.0
    total_timeout_seconds: float = 30.0
    target_parallelism: int = 2
    # Zero keeps the established serial-first behavior. A positive value is an
    # explicit experiment and must pass a full candidate regression before use.
    primary_target_head_start_seconds: float = 0.0
    continuous_alignment_review_enabled: bool = True

    def provider_ready(self, provider: str) -> bool:
        normalized = str(provider or "").strip().lower()
        if normalized == "tencent":
            return bool(self.tencent_secret_id.strip() and self.tencent_secret_key.strip())
        if normalized == "deepl":
            return bool(self.deepl_api_key.strip())
        return False

    @property
    def configured_providers(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for raw_provider in self.provider_order:
            provider = str(raw_provider or "").strip().lower()
            if provider in SUPPORTED_PROVIDERS and provider not in ordered and self.provider_ready(provider):
                ordered.append(provider)
        return tuple(ordered)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.configured_providers)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_cached_translation(
    *,
    business_db_path: str,
    owner_user_id: int,
    source_text: str,
    source_language_code: str,
    target_language_code: str,
    provider_key: str,
) -> str:
    conn = connect_business_db(business_db_path)
    try:
        row = conn.execute(
            """
            SELECT translated_text
              FROM drama_subtitle_translation_cache
             WHERE owner_user_id = ?
               AND source_text_sha256 = ?
               AND source_language_code = ?
               AND target_language_code = ?
               AND provider_key = ?
            """,
            (
                owner_user_id,
                _text_hash(source_text),
                source_language_code,
                target_language_code,
                provider_key,
            ),
        ).fetchone()
        return str(row["translated_text"] or "") if row else ""
    finally:
        conn.close()


def _save_cached_translation(
    *,
    business_db_path: str,
    owner_user_id: int,
    source_text: str,
    source_language_code: str,
    target_language_code: str,
    translated_text: str,
    provider_key: str,
) -> None:
    now = now_ts()
    conn = connect_business_db(business_db_path)
    try:
        conn.execute(
            """
            INSERT INTO drama_subtitle_translation_cache (
                owner_user_id, source_text_sha256, source_language_code,
                target_language_code, provider_key, translated_text,
                source_char_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                owner_user_id, source_text_sha256, source_language_code,
                target_language_code, provider_key
            ) DO UPDATE SET translated_text = excluded.translated_text, updated_at = excluded.updated_at
            """,
            (
                owner_user_id,
                _text_hash(source_text),
                source_language_code,
                target_language_code,
                provider_key,
                translated_text,
                len(source_text),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def translate_with_deepl(
    *,
    text: str,
    source_language_code: str,
    target_language_code: str,
    config: DramaSubtitleTranslationConfig,
    business_db_path: str,
    owner_user_id: int,
) -> dict[str, Any]:
    source_text = str(text or "").strip()
    source_language = str(source_language_code or "").strip().lower()
    target_language = str(target_language_code or "").strip().lower()
    if not source_text:
        raise DramaSubtitleTranslationError("translation source text is empty")
    if source_language not in DEEPL_SOURCE_CODES or target_language not in DEEPL_TARGET_CODES:
        raise DramaSubtitleTranslationError("unsupported translation language")
    cached = _load_cached_translation(
        business_db_path=business_db_path,
        owner_user_id=owner_user_id,
        source_text=source_text,
        source_language_code=source_language,
        target_language_code=target_language,
        provider_key=DEEPL_PROVIDER_KEY,
    )
    if cached:
        return {"text": cached, "cache_hit": True, "provider": DEEPL_PROVIDER_KEY, "duration_seconds": 0.0}
    if not config.provider_ready("deepl"):
        raise DramaSubtitleTranslationError("DeepL translation fallback is not configured")
    started_at = time.perf_counter()
    request = Request(
        config.endpoint,
        data=json.dumps(
            {
                "text": [source_text],
                "source_lang": DEEPL_SOURCE_CODES[source_language],
                "target_lang": DEEPL_TARGET_CODES[target_language],
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"DeepL-Auth-Key {config.deepl_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(float(config.timeout_seconds), 0.1)) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise DramaSubtitleTranslationError(f"DeepL request failed: {exc}") from exc
    try:
        translated_text = str(response_payload["translations"][0]["text"] or "").strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise DramaSubtitleTranslationError("DeepL returned an invalid translation response") from exc
    if not translated_text:
        raise DramaSubtitleTranslationError("DeepL returned an empty translation")
    _save_cached_translation(
        business_db_path=business_db_path,
        owner_user_id=owner_user_id,
        source_text=source_text,
        source_language_code=source_language,
        target_language_code=target_language,
        translated_text=translated_text,
        provider_key=DEEPL_PROVIDER_KEY,
    )
    return {
        "text": translated_text,
        "cache_hit": False,
        "provider": DEEPL_PROVIDER_KEY,
        "duration_seconds": round(time.perf_counter() - started_at, 4),
    }


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _tencent_authorization(
    *,
    payload: bytes,
    secret_id: str,
    secret_key: str,
    timestamp: int,
    host: str,
) -> str:
    service = "tmt"
    algorithm = "TC3-HMAC-SHA256"
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        f"{algorithm}\n{timestamp}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    secret_date = _hmac_sha256(f"TC3{secret_key}".encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def translate_with_tencent(
    *,
    text: str,
    source_language_code: str,
    target_language_code: str,
    config: DramaSubtitleTranslationConfig,
    business_db_path: str,
    owner_user_id: int,
) -> dict[str, Any]:
    source_text = str(text or "").strip()
    source_language = str(source_language_code or "").strip().lower()
    target_language = str(target_language_code or "").strip().lower()
    if not source_text:
        raise DramaSubtitleTranslationError("translation source text is empty")
    if source_language not in TENCENT_LANGUAGE_CODES or target_language not in TENCENT_LANGUAGE_CODES:
        raise DramaSubtitleTranslationError("unsupported translation language")
    cached = _load_cached_translation(
        business_db_path=business_db_path,
        owner_user_id=owner_user_id,
        source_text=source_text,
        source_language_code=source_language,
        target_language_code=target_language,
        provider_key=TENCENT_PROVIDER_KEY,
    )
    if cached:
        return {"text": cached, "cache_hit": True, "provider": TENCENT_PROVIDER_KEY, "duration_seconds": 0.0}
    if not config.provider_ready("tencent"):
        raise DramaSubtitleTranslationError("Tencent translation fallback is not configured")

    endpoint = str(config.tencent_endpoint or TENCENT_TMT_ENDPOINT).strip().rstrip("/")
    if endpoint != TENCENT_TMT_ENDPOINT:
        raise DramaSubtitleTranslationError("unsupported Tencent TMT endpoint")
    host = "tmt.tencentcloudapi.com"
    payload = json.dumps(
        {
            "SourceText": source_text,
            "Source": TENCENT_LANGUAGE_CODES[source_language],
            "Target": TENCENT_LANGUAGE_CODES[target_language],
            "ProjectId": int(config.tencent_project_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = int(time.time())
    authorization = _tencent_authorization(
        payload=payload,
        secret_id=config.tencent_secret_id.strip(),
        secret_key=config.tencent_secret_key.strip(),
        timestamp=timestamp,
        host=host,
    )
    request = Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": "TextTranslate",
            "X-TC-Version": "2018-03-21",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": config.tencent_region.strip() or "ap-shanghai",
        },
        method="POST",
    )
    started_at = time.perf_counter()
    try:
        with urlopen(request, timeout=max(float(config.timeout_seconds), 0.1)) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DramaSubtitleTranslationError(f"Tencent request failed: {exc}") from exc
    response_data = response_payload.get("Response") if isinstance(response_payload, dict) else None
    if not isinstance(response_data, dict):
        raise DramaSubtitleTranslationError("Tencent returned an invalid translation response")
    response_error = response_data.get("Error")
    if isinstance(response_error, dict):
        code = str(response_error.get("Code") or "unknown_error")
        message = str(response_error.get("Message") or "Tencent translation failed")
        raise DramaSubtitleTranslationError(f"Tencent request failed: {code}: {message}")
    translated_text = str(response_data.get("TargetText") or "").strip()
    if not translated_text:
        raise DramaSubtitleTranslationError("Tencent returned an empty translation")
    _save_cached_translation(
        business_db_path=business_db_path,
        owner_user_id=owner_user_id,
        source_text=source_text,
        source_language_code=source_language,
        target_language_code=target_language,
        translated_text=translated_text,
        provider_key=TENCENT_PROVIDER_KEY,
    )
    return {
        "text": translated_text,
        "cache_hit": False,
        "provider": TENCENT_PROVIDER_KEY,
        "duration_seconds": round(time.perf_counter() - started_at, 4),
        "request_id": str(response_data.get("RequestId") or ""),
        "used_amount": int(response_data.get("UsedAmount") or 0),
    }


def translate_with_configured_provider(
    *,
    text: str,
    source_language_code: str,
    target_language_code: str,
    config: DramaSubtitleTranslationConfig,
    business_db_path: str,
    owner_user_id: int,
) -> dict[str, Any]:
    provider_failures: list[dict[str, str]] = []
    for provider in config.configured_providers:
        try:
            if provider == "tencent":
                translated = translate_with_tencent(
                    text=text,
                    source_language_code=source_language_code,
                    target_language_code=target_language_code,
                    config=config,
                    business_db_path=business_db_path,
                    owner_user_id=owner_user_id,
                )
            else:
                translated = translate_with_deepl(
                    text=text,
                    source_language_code=source_language_code,
                    target_language_code=target_language_code,
                    config=config,
                    business_db_path=business_db_path,
                    owner_user_id=owner_user_id,
                )
            if provider_failures:
                translated["provider_failures"] = provider_failures
            return translated
        except DramaSubtitleTranslationError as exc:
            provider_failures.append({"provider": provider, "error": str(exc)})
    if provider_failures:
        detail = "; ".join(f"{item['provider']}: {item['error']}" for item in provider_failures)
        raise DramaSubtitleTranslationError(f"all translation providers failed: {detail}")
    raise DramaSubtitleTranslationError("translation fallback is not configured")


def target_languages_for(source_language_code: str) -> tuple[str, ...]:
    return TARGETS_BY_SOURCE_LANGUAGE.get(str(source_language_code or "").strip().lower(), ("zh", "en"))


SearchFunction = Callable[..., dict[str, Any]]


def _number(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _alignment_text(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).casefold()


def _candidate_evidence_text(candidate: dict[str, Any]) -> str:
    evidence = candidate.get("evidence")
    if isinstance(evidence, dict):
        return str(evidence.get("window_text") or evidence.get("window_text_preview") or "")
    return str(candidate.get("evidence_text") or "")


def _ordered_alignment(candidate: dict[str, Any], translated_text: str) -> tuple[float, int, int]:
    query = _alignment_text(translated_text)
    evidence = _alignment_text(_candidate_evidence_text(candidate))
    if not query or not evidence:
        return 0.0, 0, len(evidence)
    blocks = SequenceMatcher(None, query, evidence, autojunk=False).get_matching_blocks()
    matched_characters = sum(block.size for block in blocks)
    return matched_characters / len(evidence), matched_characters, len(evidence)


def _alignment_meets_review_threshold(
    *,
    semantic_score: float,
    alignment_score: float,
    evidence_characters: int,
    matched_characters: int,
) -> bool:
    standard_gate = (
        semantic_score >= TRANSLATION_ALIGNMENT_MIN_SEMANTIC_SCORE
        and alignment_score >= TRANSLATION_ALIGNMENT_MIN_SCORE
        and evidence_characters >= TRANSLATION_ALIGNMENT_MIN_EVIDENCE_CHARS
        and matched_characters >= TRANSLATION_ALIGNMENT_MIN_MATCHED_CHARS
    )
    # The rescue gate handles long windows: the absolute ordered overlap and
    # semantic score must both be materially stronger than the normal gate.
    rescue_gate = (
        semantic_score >= TRANSLATION_ALIGNMENT_RESCUE_MIN_SEMANTIC_SCORE
        and alignment_score >= TRANSLATION_ALIGNMENT_RESCUE_MIN_SCORE
        and evidence_characters >= TRANSLATION_ALIGNMENT_RESCUE_MIN_EVIDENCE_CHARS
        and matched_characters >= TRANSLATION_ALIGNMENT_RESCUE_MIN_MATCHED_CHARS
    )
    return standard_gate or rescue_gate


def _lexical_alignment_meets_review_threshold(
    candidate: dict[str, Any],
    *,
    alignment_score: float,
    evidence_characters: int,
    matched_characters: int,
) -> bool:
    """Keep long translated lexical dialogue evidence reviewable without a semantic call."""
    if "lexical" not in list(candidate.get("retrieval_sources") or []):
        return False
    metrics = candidate.get("match_metrics") if isinstance(candidate.get("match_metrics"), dict) else {}
    return (
        int(metrics.get("shared_trigram_count") or 0) >= TRANSLATION_LEXICAL_PRECHECK_MIN_SHARED
        and _number(metrics.get("query_coverage_rate")) >= TRANSLATION_LEXICAL_PRECHECK_MIN_QUERY_COVERAGE
        and _number(metrics.get("evidence_coverage_rate")) >= TRANSLATION_LEXICAL_PRECHECK_MIN_EVIDENCE_COVERAGE
        and alignment_score >= TRANSLATION_LEXICAL_ALIGNMENT_MIN_SCORE
        and evidence_characters >= TRANSLATION_LEXICAL_ALIGNMENT_MIN_EVIDENCE_CHARS
        and matched_characters >= TRANSLATION_LEXICAL_ALIGNMENT_MIN_MATCHED_CHARS
    )


def _translation_alignment_candidates(
    payload: dict[str, Any],
    *,
    translated_text: str,
) -> list[dict[str, Any]]:
    qualified: list[dict[str, Any]] = []
    for candidate in list(payload.get("candidates") or []):
        if not isinstance(candidate, dict) or not str(candidate.get("book_id") or ""):
            continue
        semantic_score = _number(candidate.get("semantic_score"))
        alignment_score, matched_characters, evidence_characters = _ordered_alignment(
            candidate,
            translated_text,
        )
        if _alignment_meets_review_threshold(
            semantic_score=semantic_score,
            alignment_score=alignment_score,
            evidence_characters=evidence_characters,
            matched_characters=matched_characters,
        ):
            qualified.append(
                {
                    "candidate": candidate,
                    "alignment_score": alignment_score,
                    "matched_characters": matched_characters,
                    "evidence_characters": evidence_characters,
                    "semantic_score": semantic_score,
                }
            )
    return qualified


def _translation_alignment_auto_confirmation(
    *,
    payload: dict[str, Any],
    decision: dict[str, Any],
    translated_text: str,
) -> tuple[bool, list[str]]:
    if str(decision.get("reason") or "") != "translation_continuous_alignment_requires_review":
        return False, []
    qualified = _translation_alignment_candidates(payload, translated_text=translated_text)
    book_ids = list(dict.fromkeys(str(item["candidate"].get("book_id") or "") for item in qualified))
    if len(book_ids) != 1:
        return False, book_ids
    return (
        _number(decision.get("semantic_score")) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_SEMANTIC_SCORE
        and _number(decision.get("ordered_alignment_score")) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_SCORE
        and int(decision.get("evidence_character_count") or 0) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_EVIDENCE_CHARS
        and int(decision.get("aligned_character_count") or 0) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_MATCHED_CHARS,
        book_ids,
    )


def _translation_alignment_has_content_evidence(decision: dict[str, Any]) -> bool:
    """Require a substantial block before exposing a content-level hit label."""
    return (
        str(decision.get("reason") or "") == "translation_continuous_alignment_requires_review"
        and _number(decision.get("semantic_score")) >= TRANSLATION_ALIGNMENT_MIN_SEMANTIC_SCORE
        and _number(decision.get("ordered_alignment_score")) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_SCORE
        and int(decision.get("evidence_character_count") or 0) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_EVIDENCE_CHARS
        and int(decision.get("aligned_character_count") or 0) >= TRANSLATION_ALIGNMENT_RESCUE_MIN_MATCHED_CHARS
    )


def _translation_alignment_review_decision(
    payload: dict[str, Any],
    *,
    translated_text: str,
) -> dict[str, Any] | None:
    qualified: list[tuple[float, int, float, int, bool, dict[str, Any]]] = []
    # Compare only each target language's Top1. Lower-ranked candidates can collect
    # scattered common words from a long translation and must not displace a stable result.
    candidates = [
        item
        for item in list(payload.get("candidates") or [])
        if isinstance(item, dict) and int(item.get("rank") or 0) == 1
    ]
    for candidate in candidates:
        semantic_score = _number(candidate.get("semantic_score"))
        alignment_score, matched_characters, evidence_characters = _ordered_alignment(
            candidate,
            translated_text,
        )
        semantic_gate = _alignment_meets_review_threshold(
            semantic_score=semantic_score,
            alignment_score=alignment_score,
            evidence_characters=evidence_characters,
            matched_characters=matched_characters,
        )
        lexical_gate = _lexical_alignment_meets_review_threshold(
            candidate,
            alignment_score=alignment_score,
            evidence_characters=evidence_characters,
            matched_characters=matched_characters,
        )
        if semantic_gate or lexical_gate:
            qualified.append(
                (
                    alignment_score,
                    matched_characters,
                    semantic_score,
                    -int(candidate.get("rank") or 0),
                    lexical_gate and not semantic_gate,
                    candidate,
                )
            )
    if not qualified:
        return None

    alignment_score, matched_characters, semantic_score, _, lexical_only, primary = max(
        qualified,
        key=lambda item: item[:4],
    )
    primary_book_id = str(primary.get("book_id") or "")
    evidence = primary.get("evidence") if isinstance(primary.get("evidence"), dict) else {}
    metrics = primary.get("match_metrics") if isinstance(primary.get("match_metrics"), dict) else {}
    same_book_count = sum(1 for candidate in candidates if str(candidate.get("book_id") or "") == primary_book_id)
    return {
        "matched": False,
        "status": "review_required",
        "outcome": "potential_match",
        "content_match_status": "uncertain",
        "title_resolution": "unresolved",
        "reason": (
            "translation_lexical_continuous_alignment_requires_review"
            if lexical_only
            else "translation_continuous_alignment_requires_review"
        ),
        "user_message": (
            "翻译后发现较长的同序词法对白证据，需要人工确认是否为同一内容的跨语言版本。"
            if lexical_only
            else "翻译后发现较长的同序对白证据，需要人工确认是否为同一内容的跨语言版本。"
        ),
        "candidate_rank": primary.get("rank"),
        "book_id": primary_book_id,
        "book_name": str(primary.get("book_name") or ""),
        "matched_episode_order": primary.get("episode_order"),
        "language_code": str(primary.get("language_code") or ""),
        "semantic_score": primary.get("semantic_score"),
        "text_coverage_rate": metrics.get("query_coverage_rate"),
        "aggregate_text_coverage_rate": metrics.get("retrieved_window_query_coverage_rate"),
        "evidence_coverage_rate": metrics.get("evidence_coverage_rate"),
        "shared_match_count": metrics.get("shared_trigram_count"),
        "match_unit_label": metrics.get("match_unit_label"),
        "ordered_alignment_score": round(alignment_score, 4),
        "aligned_character_count": matched_characters,
        "evidence_character_count": len(_alignment_text(_candidate_evidence_text(primary))),
        "same_book_candidate_count": same_book_count,
        "evidence_window_uid": str(evidence.get("window_uid") or primary.get("evidence_window_uid") or ""),
        "review_feedback": {
            "reason_type": (
                "translation_lexical_continuous_dialogue_alignment"
                if lexical_only
                else "translation_continuous_dialogue_alignment"
            ),
            "title": "翻译后发现连续同序词法对白" if lexical_only else "翻译后发现连续同序对白",
            "summary": (
                f"候选证据与翻译字幕的同序对齐率为 {alignment_score:.1%}，"
                f"连续对齐字符数为 {matched_characters}，语义相似度为 {semantic_score:.3f}。"
            ),
            "recommended_action": "请核对角色名称替换、事件顺序和前后对白，确认是否为同内容的不同语言版本。",
            "signals": [
                {"label": "同序对齐率", "value": f"{alignment_score:.1%}"},
                {"label": "对齐字符", "value": str(matched_characters)},
                *([] if lexical_only else [{"label": "语义相似度", "value": f"{semantic_score:.3f}"}]),
            ],
        },
    }


def _decision_with_alignment(
    decision: dict[str, Any],
    payload: dict[str, Any],
    *,
    translated_text: str,
) -> dict[str, Any]:
    book_id = str(decision.get("book_id") or "")
    if not book_id:
        return decision
    aligned: list[tuple[float, int, int]] = []
    for candidate in list(payload.get("candidates") or []):
        if isinstance(candidate, dict) and str(candidate.get("book_id") or "") == book_id:
            aligned.append(_ordered_alignment(candidate, translated_text))
    if not aligned:
        return decision
    score, matched_characters, evidence_characters = max(aligned, key=lambda item: item[:2])
    return {
        **decision,
        "ordered_alignment_score": round(score, 4),
        "aligned_character_count": matched_characters,
        "evidence_character_count": evidence_characters,
    }


def _compact_translation_candidates(payload: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for candidate in list(payload.get("candidates") or [])[:limit]:
        if not isinstance(candidate, dict):
            continue
        metrics = candidate.get("match_metrics") if isinstance(candidate.get("match_metrics"), dict) else {}
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
        compact.append(
            {
                "rank": candidate.get("rank"),
                "book_id": str(candidate.get("book_id") or ""),
                "book_name": str(candidate.get("book_name") or ""),
                "episode_order": candidate.get("episode_order"),
                "language_code": str(candidate.get("language_code") or ""),
                "semantic_score": candidate.get("semantic_score"),
                "retrieval_sources": list(candidate.get("retrieval_sources") or []),
                "shared_match_count": metrics.get("shared_trigram_count"),
                "query_coverage_rate": metrics.get("query_coverage_rate"),
                "evidence_coverage_rate": metrics.get("evidence_coverage_rate"),
                "evidence_window_uid": str(evidence.get("window_uid") or ""),
                "evidence_text": str(evidence.get("window_text") or evidence.get("window_text_preview") or "")[:1200],
            }
        )
    return compact


def _translation_semantic_review_decision(payload: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [item for item in list(payload.get("candidates") or []) if isinstance(item, dict)]
    semantic_candidates = [item for item in candidates if item.get("semantic_score") is not None]
    if not semantic_candidates:
        return None

    by_book: dict[str, list[dict[str, Any]]] = {}
    for candidate in semantic_candidates:
        book_id = str(candidate.get("book_id") or "")
        if book_id:
            by_book.setdefault(book_id, []).append(candidate)
    if not by_book:
        return None

    best_by_book = [max(items, key=lambda item: _number(item.get("semantic_score"))) for items in by_book.values()]
    best_by_book.sort(key=lambda item: _number(item.get("semantic_score")), reverse=True)
    primary = best_by_book[0]
    primary_book_id = str(primary.get("book_id") or "")
    primary_score = _number(primary.get("semantic_score"))
    runner_score = _number(best_by_book[1].get("semantic_score")) if len(best_by_book) > 1 else 0.0
    margin = primary_score - runner_score
    support_count = len(by_book.get(primary_book_id) or [])
    metrics = primary.get("match_metrics") if isinstance(primary.get("match_metrics"), dict) else {}
    shared_count = int(metrics.get("shared_trigram_count") or 0)
    sources = list(primary.get("retrieval_sources") or [])
    has_support = (
        margin >= TRANSLATION_SEMANTIC_REVIEW_MIN_MARGIN
        or support_count >= 2
        or ("lexical" in sources and shared_count >= 6 and margin >= 0.01)
    )
    if primary_score < TRANSLATION_SEMANTIC_REVIEW_MIN_SCORE or not has_support:
        return None

    evidence = primary.get("evidence") if isinstance(primary.get("evidence"), dict) else {}
    return {
        "matched": False,
        "status": "review_required",
        "outcome": "potential_match",
        "content_match_status": "uncertain",
        "title_resolution": "unresolved",
        "reason": "translation_semantic_candidate_requires_review",
        "user_message": "翻译后二次检索发现稳定的高分语义候选，需要结合翻译文本和原版字幕人工复核。",
        "candidate_rank": primary.get("rank"),
        "book_id": primary_book_id,
        "book_name": str(primary.get("book_name") or ""),
        "matched_episode_order": primary.get("episode_order"),
        "language_code": str(primary.get("language_code") or ""),
        "semantic_score": primary.get("semantic_score"),
        "semantic_margin": round(margin, 4),
        "same_book_candidate_count": support_count,
        "evidence_window_uid": str(evidence.get("window_uid") or ""),
        "review_feedback": {
            "reason_type": "translation_semantic_candidate_requires_review",
            "title": "翻译后发现稳定语义候选",
            "summary": (
                f"候选语义分数为 {primary_score:.3f}，与最佳其他剧相差 {margin:.3f}，"
                f"同一剧在候选列表中出现 {support_count} 次。"
            ),
            "recommended_action": "请核对翻译字幕与候选剧的连续剧情、人物关系和上下文证据。",
            "signals": [
                {"label": "语义相似度", "value": f"{primary_score:.3f}"},
                {"label": "候选分差", "value": f"{margin:.3f}"},
                {"label": "同剧候选数", "value": str(support_count)},
            ],
        },
    }


def _translation_review_quality(decision: dict[str, Any]) -> tuple[int, float, int, float, float]:
    content_matched = str(decision.get("content_match_status") or "") == "matched"
    feedback = decision.get("review_feedback") if isinstance(decision.get("review_feedback"), dict) else {}
    reason_type = str(feedback.get("reason_type") or decision.get("reason") or "")
    is_alignment_review = "continuous_dialogue_alignment" in reason_type
    evidence_priority = (
        3
        if content_matched
        else 2
        if is_alignment_review
        else 1
        if "lexical" in reason_type
        else 0
    )
    return (
        evidence_priority,
        _number(decision.get("ordered_alignment_score")) if is_alignment_review else 0.0,
        int(decision.get("same_book_candidate_count") or 0),
        _number(decision.get("semantic_margin")),
        _number(decision.get("semantic_score")),
    )


def _is_high_confidence_alignment(decision: dict[str, Any]) -> bool:
    """Identify a strong translated dialogue block before comparing weak ranks."""
    return (
        str(decision.get("reason") or "") == "translation_continuous_alignment_requires_review"
        and _number(decision.get("semantic_score")) >= TRANSLATION_ALIGNMENT_MIN_SEMANTIC_SCORE
        and _number(decision.get("ordered_alignment_score")) >= 0.65
        and int(decision.get("aligned_character_count") or 0) >= 100
        and int(decision.get("evidence_character_count") or 0) >= 150
    )


def _content_candidate_options(deferred_reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose evidence-ranked candidates without treating a Book ID as ground truth."""
    ordered = sorted(
        deferred_reviews,
        key=lambda item: _translation_review_quality(item["translated_decision"]),
        reverse=True,
    )
    options: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in ordered:
        decision = item["translated_decision"]
        book_id = str(decision.get("book_id") or "")
        target_language = str(item.get("target_language") or "")
        key = (book_id, target_language)
        if not book_id or key in seen:
            continue
        seen.add(key)
        feedback = decision.get("review_feedback") if isinstance(decision.get("review_feedback"), dict) else {}
        options.append(
            {
                "review_priority": len(options) + 1,
                "book_id": book_id,
                "book_name": str(decision.get("book_name") or ""),
                "matched_episode_order": decision.get("matched_episode_order"),
                "target_language_code": target_language,
                "candidate_language_code": str(decision.get("language_code") or ""),
                "candidate_rank": decision.get("candidate_rank"),
                "evidence_window_uid": str(decision.get("evidence_window_uid") or ""),
                "evidence_type": str(feedback.get("reason_type") or decision.get("reason") or ""),
                "ordered_alignment_score": decision.get("ordered_alignment_score"),
                "aligned_character_count": decision.get("aligned_character_count"),
                "shared_match_count": decision.get("shared_match_count"),
                "text_coverage_rate": decision.get("text_coverage_rate"),
                "evidence_coverage_rate": decision.get("evidence_coverage_rate"),
                "semantic_score": decision.get("semantic_score"),
            }
        )
        if len(options) >= 5:
            break
    return options


def _select_translation_review(deferred_reviews: list[dict[str, Any]]) -> dict[str, Any]:
    alignment_reviews: list[dict[str, Any]] = []
    legacy_reviews: list[dict[str, Any]] = []
    for item in deferred_reviews:
        decision = item["translated_decision"]
        if str(decision.get("reason") or "") == "translation_continuous_alignment_requires_review":
            alignment_reviews.append(item)
        else:
            legacy_reviews.append(item)

    best_alignment = (
        max(alignment_reviews, key=lambda item: _translation_review_quality(item["translated_decision"]))
        if alignment_reviews
        else None
    )
    if not legacy_reviews:
        return best_alignment or deferred_reviews[0]

    high_confidence_alignment = [
        item
        for item in alignment_reviews
        if _is_high_confidence_alignment(item["translated_decision"])
    ]
    if high_confidence_alignment:
        return max(
            high_confidence_alignment,
            key=lambda item: _translation_review_quality(item["translated_decision"]),
        )

    best_legacy = max(
        legacy_reviews,
        key=lambda item: _translation_review_quality(item["translated_decision"]),
    )
    if best_alignment is None:
        return best_legacy

    legacy_score = _number(best_legacy["translated_decision"].get("ordered_alignment_score"))
    alignment_score = _number(best_alignment["translated_decision"].get("ordered_alignment_score"))
    if (
        legacy_score < TRANSLATION_ALIGNMENT_MAX_LEGACY_SCORE
        and alignment_score >= TRANSLATION_ALIGNMENT_MIN_SCORE
        and alignment_score - legacy_score >= TRANSLATION_ALIGNMENT_MIN_IMPROVEMENT
    ):
        return best_alignment
    return best_legacy


def _build_translation_assisted_result(
    *,
    native_payload: dict[str, Any],
    translated_payload: dict[str, Any],
    translated_decision: dict[str, Any],
    translated: dict[str, Any],
    fallback: dict[str, Any],
    source_language: str,
    target_language: str,
    content_candidate_options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    translated_result = dict(translated_payload)
    candidate_name = str(translated_decision.get("book_name") or "候选剧集")
    alignment_confirmed, alignment_book_ids = _translation_alignment_auto_confirmation(
        payload=translated_payload,
        decision=translated_decision,
        translated_text=str(translated.get("text") or ""),
    )
    is_alignment_evidence = str(translated_decision.get("reason") or "") == (
        "translation_continuous_alignment_requires_review"
    )
    is_lexical_alignment_evidence = str(translated_decision.get("reason") or "") == (
        "translation_lexical_continuous_alignment_requires_review"
    )
    content_matched_by_alignment = is_alignment_evidence and _translation_alignment_has_content_evidence(
        translated_decision
    )
    title_is_ambiguous = content_matched_by_alignment and len(alignment_book_ids) > 1
    if alignment_confirmed:
        decision_reason = "translation_continuous_alignment_confirmed"
        decision_outcome = "confirmed_match"
        decision_message = (
            "已确认命中：翻译后的字幕与库内候选存在较长、同顺序的连续对白，"
            "且语义相似度和证据强度均达到自动确认标准。"
        )
    elif title_is_ambiguous:
        decision_reason = "translation_multiple_strong_alignment_candidates"
        decision_outcome = "content_matched_ambiguous"
        decision_message = (
            f"内容已命中，但存在 {len(alignment_book_ids)} 个候选剧名或版本具有强连续对白证据，"
            "需要人工确认具体剧名或版本。"
        )
    elif content_matched_by_alignment:
        decision_reason = "translation_continuous_alignment_requires_review"
        decision_outcome = "translation_assisted_match"
        decision_message = (
            "翻译后的字幕与候选存在连续对白证据，内容可能已命中；但证据强度尚未达到自动确认标准，"
            "请结合完整上下文复核。"
        )
    elif is_lexical_alignment_evidence:
        decision_reason = "translation_lexical_continuous_alignment_requires_review"
        decision_outcome = "translation_assisted_match"
        decision_message = (
            "翻译后的字幕与候选存在较长的连续词法对白证据；当前未额外调用语义检索，"
            "请结合完整上下文复核。"
        )
    else:
        decision_reason = "translation_assisted_candidate_requires_review"
        decision_outcome = "translation_assisted_match"
        decision_message = (
            f"仅发现跨语言候选，尚未确认命中：原字幕翻译为{target_language}后出现《{candidate_name}》。"
            "机器翻译不能单独作为命中依据，请核对原字幕剧情与候选证据。"
        )
    if alignment_confirmed:
        feedback_reason_type = "translation_continuous_dialogue_confirmed"
        feedback_title = "翻译后连续对白达到自动确认标准"
        feedback_action = "可直接进入已确认命中；后续复核可继续核对完整上下文。"
    elif title_is_ambiguous:
        feedback_reason_type = "translation_multiple_strong_alignment_candidates"
        feedback_title = "内容已命中，但剧名或版本存在歧义"
        feedback_action = "请在候选剧名和版本中确认实际对应项，不要依据 Book ID 直接下结论。"
    elif content_matched_by_alignment:
        feedback_reason_type = "translation_continuous_dialogue_alignment"
        feedback_title = "发现翻译后的连续对白证据"
        feedback_action = "请核对人物关系、事件顺序和前后对白，确认是否为同一内容的不同语言版本。"
    elif is_lexical_alignment_evidence:
        feedback_reason_type = "translation_lexical_continuous_dialogue_alignment"
        feedback_title = "发现翻译后的连续词法对白"
        feedback_action = "请核对人物关系、事件顺序和前后对白；该结果由强词法证据触发，未额外调用语义检索。"
    else:
        feedback_reason_type = "translation_assisted_cross_language_candidate"
        feedback_title = "发现跨语言候选（尚未确认命中）"
        feedback_action = "请对照原字幕剧情、人物关系和候选字幕上下文；如果内容明显不对应，应保留为未命中。"
    translated_result["query_text"] = native_payload.get("query_text") or ""
    translated_result["query_language_code"] = source_language
    translated_result["query_language_confidence"] = native_payload.get("query_language_confidence") or 0.0
    translated_result["translated_query_text"] = translated["text"]
    translated_result["translation_target_language_code"] = target_language
    translated_result["translation_fallback"] = {
        **fallback,
        "status": "matched",
        "match_status": "matched" if alignment_confirmed else "review_required",
        "matched_target_language_code": target_language,
    }
    translated_result["decision"] = {
        **translated_decision,
        "matched": alignment_confirmed,
        "status": "matched" if alignment_confirmed else "review_required",
        "hit_status": "matched" if alignment_confirmed else "review_required",
        "is_confirmed_match": alignment_confirmed,
        "outcome": decision_outcome,
        "content_match_status": "matched" if content_matched_by_alignment else "uncertain",
        "title_resolution": (
            "ambiguous" if title_is_ambiguous else "unique" if content_matched_by_alignment else "unresolved"
        ),
        "reason": decision_reason,
        "user_message": decision_message,
        "translation_alignment_book_ids": alignment_book_ids,
        "translation_alignment_auto_confirmed": alignment_confirmed,
        "content_candidate_options": list(content_candidate_options or []),
        "review_feedback": {
            **(
                translated_decision.get("review_feedback")
                if isinstance(translated_decision.get("review_feedback"), dict)
                else {}
            ),
            "reason_type": feedback_reason_type,
            "title": feedback_title,
            "recommended_action": feedback_action,
            "signals": [
                {"label": "原字幕语言", "value": source_language},
                {"label": "检索语言", "value": target_language},
                {"label": "翻译来源", "value": translated["provider"]},
                *list(
                    (
                        translated_decision.get("review_feedback")
                        if isinstance(translated_decision.get("review_feedback"), dict)
                        else {}
                    ).get("signals")
                    or []
                ),
            ],
        },
    }
    return translated_result


def _translation_search_kwargs(search_kwargs: dict[str, Any]) -> dict[str, Any]:
    translated_kwargs = dict(search_kwargs)
    semantic_config = translated_kwargs.get("semantic_config")
    if (
        semantic_config is not None
        and hasattr(semantic_config, "query_window_size")
        and hasattr(semantic_config, "query_overlap_size")
    ):
        try:
            translated_kwargs["semantic_config"] = replace(
                semantic_config,
                query_window_size=min(
                    int(getattr(semantic_config, "query_window_size")),
                    TRANSLATION_SEMANTIC_QUERY_WINDOW_SIZE,
                ),
                query_overlap_size=min(
                    int(getattr(semantic_config, "query_overlap_size")),
                    TRANSLATION_SEMANTIC_QUERY_OVERLAP_SIZE,
                ),
            )
        except (TypeError, ValueError):
            pass
    return translated_kwargs


def _translation_lexical_precheck_has_high_signal(payload: dict[str, Any]) -> bool:
    top_candidate = None
    for candidate in list(payload.get("candidates") or []):
        if isinstance(candidate, dict) and int(candidate.get("rank") or 0) == 1:
            top_candidate = candidate
            break
    if not isinstance(top_candidate, dict):
        return False
    if "lexical" not in list(top_candidate.get("retrieval_sources") or []):
        return False
    metrics = top_candidate.get("match_metrics") if isinstance(top_candidate.get("match_metrics"), dict) else {}
    return (
        int(metrics.get("shared_trigram_count") or 0) >= TRANSLATION_LEXICAL_PRECHECK_MIN_SHARED
        and _number(metrics.get("query_coverage_rate")) >= TRANSLATION_LEXICAL_PRECHECK_MIN_QUERY_COVERAGE
        and _number(metrics.get("evidence_coverage_rate")) >= TRANSLATION_LEXICAL_PRECHECK_MIN_EVIDENCE_COVERAGE
    )


def _translation_requires_hybrid_followup(
    *,
    lexical_payload: dict[str, Any],
    lexical_decision: dict[str, Any],
    semantic_enabled: bool,
) -> bool:
    if not semantic_enabled:
        return False
    if str(lexical_decision.get("content_match_status") or "") == "matched":
        return False
    if _translation_lexical_precheck_has_high_signal(lexical_payload):
        return False
    return str(lexical_decision.get("outcome") or "no_match") == "no_match"


def _run_translation_target_attempt(
    *,
    target_language: str,
    source_language: str,
    query_text: str,
    deadline: float,
    search_function: SearchFunction,
    search_kwargs: dict[str, Any],
    config: DramaSubtitleTranslationConfig,
    business_db_path: str,
    owner_user_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one target-language fallback without changing final review ordering."""
    started_at = time.perf_counter()
    attempt: dict[str, Any] = {"target_language_code": target_language}
    remaining_seconds = deadline - started_at
    if remaining_seconds <= 0:
        attempt.update(
            {
                "outcome": "budget_exhausted",
                "error": "Translation fallback reached its total time budget.",
                "duration_seconds": 0.0,
            }
        )
        return attempt, []
    try:
        translated = translate_with_configured_provider(
            text=query_text,
            source_language_code=source_language,
            target_language_code=target_language,
            config=replace(
                config,
                timeout_seconds=min(float(config.timeout_seconds), remaining_seconds),
            ),
            business_db_path=business_db_path,
            owner_user_id=owner_user_id,
        )
        attempt.update(
            {
                "provider": translated["provider"],
                "cache_hit": translated["cache_hit"],
                "translation_duration_seconds": translated["duration_seconds"],
                "translated_text": translated["text"],
            }
        )
        if translated.get("provider_failures"):
            attempt["provider_failures"] = translated["provider_failures"]
        translated_search_kwargs = _translation_search_kwargs(search_kwargs)
        lexical_search_started_at = time.perf_counter()
        lexical_payload = search_function(
            query_text=str(translated["text"]),
            language_code=target_language,
            **{**translated_search_kwargs, "semantic_enabled": False},
        )
        attempt["lexical_precheck_duration_seconds"] = round(time.perf_counter() - lexical_search_started_at, 4)
        attempt["lexical_index_variant"] = lexical_payload.get("lexical_index_variant", "")
        lexical_decision = lexical_payload.get("decision") if isinstance(lexical_payload.get("decision"), dict) else {}
        semantic_enabled = bool(translated_search_kwargs.get("semantic_enabled"))
        translated_payload = lexical_payload
        translated_decision = lexical_decision
        if _translation_requires_hybrid_followup(
            lexical_payload=lexical_payload,
            lexical_decision=lexical_decision,
            semantic_enabled=semantic_enabled,
        ):
            hybrid_search_started_at = time.perf_counter()
            translated_payload = search_function(
                query_text=str(translated["text"]),
                language_code=target_language,
                **{**translated_search_kwargs, "precomputed_lexical": lexical_payload},
            )
            attempt["search_strategy"] = "lexical_then_hybrid"
            attempt["hybrid_search_duration_seconds"] = round(
                time.perf_counter() - hybrid_search_started_at,
                4,
            )
            attempt["hybrid_lexical_reused"] = bool(translated_payload.get("lexical_reused"))
            attempt["hybrid_lexical_index_variant"] = translated_payload.get(
                "lexical_index_variant", ""
            )
            attempt["hybrid_semantic_duration_seconds"] = translated_payload.get("semantic_duration_seconds", 0.0)
            attempt["hybrid_embedding_duration_seconds"] = translated_payload.get(
                "semantic_embedding_duration_seconds", 0.0
            )
            attempt["hybrid_qdrant_duration_seconds"] = translated_payload.get(
                "semantic_qdrant_duration_seconds", 0.0
            )
            attempt["hybrid_qdrant_queue_wait_seconds"] = translated_payload.get(
                "semantic_qdrant_queue_wait_seconds", 0.0
            )
            attempt["hybrid_evidence_fetch_duration_seconds"] = translated_payload.get(
                "semantic_evidence_fetch_duration_seconds", 0.0
            )
            attempt["hybrid_evidence_window_count"] = translated_payload.get(
                "semantic_evidence_window_count", 0
            )
            if translated_payload.get("semantic_error"):
                attempt["hybrid_semantic_error"] = translated_payload["semantic_error"]
            translated_decision = (
                translated_payload.get("decision")
                if isinstance(translated_payload.get("decision"), dict)
                else {}
            )
        else:
            attempt["search_strategy"] = "lexical_only"
            attempt["semantic_skipped_reason"] = (
                "semantic_disabled"
                if not semantic_enabled
                else "lexical_precheck_already_has_content_evidence"
            )
        alignment_decision = (
            _translation_alignment_review_decision(
                translated_payload,
                translated_text=str(translated["text"]),
            )
            if config.continuous_alignment_review_enabled
            and str(translated_decision.get("content_match_status") or "") != "matched"
            else None
        )
        if str(translated_decision.get("outcome") or "no_match") == "no_match":
            promoted_decision = _translation_semantic_review_decision(translated_payload)
            if promoted_decision is not None:
                translated_decision = promoted_decision
        translated_decision = _decision_with_alignment(
            translated_decision,
            translated_payload,
            translated_text=str(translated["text"]),
        )
        review_decisions: list[dict[str, Any]] = []
        if str(translated_decision.get("outcome") or "no_match") != "no_match":
            review_decisions.append(translated_decision)
        if alignment_decision is not None:
            if str(alignment_decision.get("book_id") or "") == str(translated_decision.get("book_id") or ""):
                review_decisions = [alignment_decision]
            else:
                review_decisions.append(alignment_decision)
        effective_decision = review_decisions[0] if review_decisions else translated_decision
        attempt["outcome"] = str(effective_decision.get("outcome") or "no_match")
        attempt["candidate_count"] = int(translated_payload.get("candidate_count") or 0)
        attempt["candidates"] = _compact_translation_candidates(translated_payload)
        attempt["decision"] = dict(effective_decision)
        if alignment_decision is not None and alignment_decision is not effective_decision:
            attempt["alignment_review_candidate"] = dict(alignment_decision)
        attempt["duration_seconds"] = round(time.perf_counter() - started_at, 4)
        reviews = [
            {
                "translated_payload": translated_payload,
                "translated_decision": review_decision,
                "translated": translated,
                "target_language": target_language,
            }
            for review_decision in review_decisions
        ]
        return attempt, reviews
    except DramaSubtitleTranslationError as exc:
        attempt.update(
            {
                "outcome": "translation_error",
                "error": str(exc),
                "duration_seconds": round(time.perf_counter() - started_at, 4),
            }
        )
        return attempt, []


def apply_translation_fallback(
    *,
    native_payload: dict[str, Any],
    query_text: str,
    search_function: SearchFunction,
    search_kwargs: dict[str, Any],
    config: DramaSubtitleTranslationConfig,
    business_db_path: str,
    owner_user_id: int,
) -> dict[str, Any]:
    """Try corpus-backed target languages only after the original subtitle misses."""
    result = dict(native_payload)
    native_decision = native_payload.get("decision") if isinstance(native_payload.get("decision"), dict) else {}
    source_language = str(native_payload.get("query_language_code") or "unknown").lower()
    fallback: dict[str, Any] = {
        "enabled": bool(config.enabled),
        "source_language_code": source_language,
        "attempts": [],
        "status": "not_needed",
        "budget_seconds": max(float(config.total_timeout_seconds), 0.1),
    }
    if str(native_decision.get("content_match_status") or "") == "matched":
        result["translation_fallback"] = fallback
        return result
    if not config.ready:
        fallback["status"] = "unavailable"
        fallback["message"] = "原语言未命中，翻译回退未配置或暂不可用。"
        result["translation_fallback"] = fallback
        return result

    fallback["status"] = "attempted"
    deferred_reviews: list[dict[str, Any]] = []
    deadline = time.perf_counter() + max(float(config.total_timeout_seconds), 0.1)
    target_languages = list(target_languages_for(source_language))
    fallback["target_parallelism"] = max(1, int(config.target_parallelism or 1))
    fallback["primary_target_head_start_seconds"] = max(
        float(config.primary_target_head_start_seconds or 0.0),
        0.0,
    )
    fallback["primary_target_overlapped"] = False

    def collect_attempt(attempt: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
        fallback["attempts"].append(attempt)
        if attempt["outcome"] == "budget_exhausted":
            fallback["status"] = "budget_exhausted"
            fallback["message"] = "Translation fallback stopped after reaching its total time budget."
            return None
        if attempt["outcome"] == "translation_error":
            return None
        for review in reviews:
            decision = review["translated_decision"]
            if str(decision.get("content_match_status") or "") == "matched":
                return review
            deferred_reviews.append(review)
        return None

    def build_immediate_result(review: dict[str, Any]) -> dict[str, Any]:
        return _build_translation_assisted_result(
            native_payload={**native_payload, "query_text": native_payload.get("query_text") or query_text},
            translated_payload=review["translated_payload"],
            translated_decision=review["translated_decision"],
            translated=review["translated"],
            fallback=fallback,
            source_language=source_language,
            target_language=review["target_language"],
            content_candidate_options=_content_candidate_options([review]),
        )

    # Keep a short head start for the highest-priority language so fast, strong
    # confirmations retain their current low latency. When it is slow, overlap
    # the remaining languages instead of idling before their costly retrievals.
    if target_languages:
        first_target = target_languages[0]
        remaining_targets = target_languages[1:]

        def run_target(target_language: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return _run_translation_target_attempt(
                target_language=target_language,
                source_language=source_language,
                query_text=query_text,
                deadline=deadline,
                search_function=search_function,
                search_kwargs=search_kwargs,
                config=config,
                business_db_path=business_db_path,
                owner_user_id=owner_user_id,
            )

        ordered_attempts: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        worker_count = min(fallback["target_parallelism"], len(target_languages))
        if not remaining_targets or worker_count == 1:
            ordered_attempts.append(run_target(first_target))
            first_attempt = ordered_attempts[0][0]
            if first_attempt["outcome"] not in {"translation_error", "budget_exhausted"}:
                ordered_attempts.extend(run_target(target) for target in remaining_targets)
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="subtitle-translation") as executor:
                first_future = executor.submit(run_target, first_target)
                primary_head_start_timeout = fallback["primary_target_head_start_seconds"]
                if primary_head_start_timeout <= 0:
                    primary_head_start_timeout = max(deadline - time.perf_counter(), 0.1)
                try:
                    first_result = first_future.result(
                        timeout=primary_head_start_timeout,
                    )
                except FuturesTimeoutError:
                    fallback["primary_target_overlapped"] = True
                    futures = {first_target: first_future}
                    futures.update(
                        {target: executor.submit(run_target, target) for target in remaining_targets}
                    )
                    ordered_attempts = [futures[target].result() for target in target_languages]
                else:
                    first_attempt = first_result[0]
                    immediate_review = collect_attempt(*first_result)
                    if immediate_review is not None:
                        return build_immediate_result(immediate_review)
                    if first_attempt["outcome"] not in {"translation_error", "budget_exhausted"}:
                        futures = [executor.submit(run_target, target) for target in remaining_targets]
                        ordered_attempts = [future.result() for future in futures]

        for attempt, reviews in ordered_attempts:
            immediate_review = collect_attempt(attempt, reviews)
            if immediate_review is not None:
                return build_immediate_result(immediate_review)
            if attempt["outcome"] in {"translation_error", "budget_exhausted"}:
                break

    if deferred_reviews:
        ordered_reviews = sorted(
            deferred_reviews,
            key=lambda item: _translation_review_quality(item["translated_decision"]),
            reverse=True,
        )
        selected = _select_translation_review(ordered_reviews)
        return _build_translation_assisted_result(
            native_payload={**native_payload, "query_text": native_payload.get("query_text") or query_text},
            translated_payload=selected["translated_payload"],
            translated_decision=selected["translated_decision"],
            translated=selected["translated"],
            fallback=fallback,
            source_language=source_language,
            target_language=selected["target_language"],
            content_candidate_options=_content_candidate_options(ordered_reviews),
        )
    if fallback["status"] == "attempted":
        fallback["status"] = "exhausted"
    result["translation_fallback"] = fallback
    return result
