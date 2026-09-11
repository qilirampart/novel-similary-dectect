from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any


MATCHED = "matched"
REVIEW_REQUIRED = "review_required"
NOT_MATCHED = "not_matched"


def _number(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metrics(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = candidate.get("match_metrics")
    return metrics if isinstance(metrics, dict) else {}


def _has_lexical_evidence(candidate: dict[str, Any]) -> bool:
    return "lexical" in list(candidate.get("retrieval_sources") or [])


def _shared_count(candidate: dict[str, Any]) -> int:
    return _integer(_metrics(candidate).get("shared_trigram_count"))


def _query_count(candidate: dict[str, Any]) -> int:
    return _integer(_metrics(candidate).get("query_trigram_count"))


def _query_coverage(candidate: dict[str, Any]) -> float:
    return _number(_metrics(candidate).get("query_coverage_rate"))


def _retrieved_window_query_coverage(candidate: dict[str, Any]) -> float:
    metrics = _metrics(candidate)
    return _number(metrics.get("retrieved_window_query_coverage_rate") or metrics.get("query_coverage_rate"))


def _evidence_coverage(candidate: dict[str, Any]) -> float:
    return _number(_metrics(candidate).get("evidence_coverage_rate"))


def _semantic_score(candidate: dict[str, Any]) -> float:
    return _number(candidate.get("semantic_score"))


def _evidence_text(candidate: dict[str, Any]) -> str:
    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    return str(evidence.get("window_text") or evidence.get("window_text_preview") or "")


def _alignment_text(value: object) -> str:
    """Keep ordered CJK and alphanumeric content while ignoring caption punctuation."""
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).casefold()


def _ordered_fuzzy_alignment(candidate: dict[str, Any], query_text: str) -> float:
    query = _alignment_text(query_text)
    evidence = _alignment_text(_evidence_text(candidate))
    if not query or not evidence:
        return 0.0
    blocks = SequenceMatcher(None, query, evidence, autojunk=False).get_matching_blocks()
    matched_characters = sum(block.size for block in blocks)
    return matched_characters / len(evidence)


def _is_strong_lexical(candidate: dict[str, Any]) -> bool:
    if not _has_lexical_evidence(candidate):
        return False
    required_shared = max(8, math.ceil(_query_count(candidate) * 0.08))
    return (
        _shared_count(candidate) >= required_shared
        and _evidence_coverage(candidate) >= 0.60
        and _query_coverage(candidate) >= 0.06
    )


def _is_weak_lexical(candidate: dict[str, Any]) -> bool:
    return (
        _has_lexical_evidence(candidate)
        and _shared_count(candidate) >= 4
        and _evidence_coverage(candidate) >= 0.15
    )


def _is_chinese_fuzzy_lexical_match(
    candidate: dict[str, Any],
    *,
    query_text: str,
    query_language_code: str,
) -> bool:
    """Confirm close Chinese ASR/transcription variants without relaxing semantic-only results."""
    metrics = _metrics(candidate)
    return (
        str(query_language_code or "").strip().lower() == "zh"
        and str(candidate.get("language_code") or "").strip().lower() == "zh"
        and _has_lexical_evidence(candidate)
        and str(metrics.get("match_unit") or "") == "character_trigram"
        and _shared_count(candidate) >= 16
        and _evidence_coverage(candidate) >= 0.30
        and _ordered_fuzzy_alignment(candidate, query_text) >= 0.78
    )


def _is_same_language(candidate: dict[str, Any], query_language_code: str) -> bool:
    query_language = str(query_language_code or "").strip().lower()
    candidate_language = str(candidate.get("language_code") or "").strip().lower()
    return not query_language or query_language == "unknown" or candidate_language == query_language


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, float, int, float, float, int]:
    return (
        _retrieved_window_query_coverage(candidate),
        _query_coverage(candidate),
        _shared_count(candidate),
        _evidence_coverage(candidate),
        _semantic_score(candidate),
        -_integer(candidate.get("rank")),
    )


def _candidate_reference(candidate: dict[str, Any]) -> dict[str, Any]:
    evidence = candidate.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return {
        "candidate_rank": _integer(candidate.get("rank")),
        "book_id": str(candidate.get("book_id") or ""),
        "book_name": str(candidate.get("book_name") or ""),
        "matched_episode_order": _integer(candidate.get("episode_order")) or None,
        "language_code": str(candidate.get("language_code") or ""),
        "evidence_window_uid": str(evidence.get("window_uid") or ""),
        # Keep text_coverage_rate backward-compatible: it is the best-window value.
        "text_coverage_rate": _query_coverage(candidate),
        "aggregate_text_coverage_rate": _retrieved_window_query_coverage(candidate),
        "matched_window_count": _integer(candidate.get("retrieved_window_count")),
        "evidence_coverage_rate": _evidence_coverage(candidate),
        "shared_match_count": _shared_count(candidate),
        "semantic_score": candidate.get("semantic_score"),
        "match_unit_label": str(_metrics(candidate).get("match_unit_label") or ""),
    }


def _strong_candidate_references(strong_by_book: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    strongest_by_book = [max(items, key=_candidate_sort_key) for items in strong_by_book.values()]
    ordered = sorted(strongest_by_book, key=_candidate_sort_key, reverse=True)
    return [
        {
            **_candidate_reference(candidate),
            # The review order favors directly reusable text over semantic proximity.
            "review_priority": index,
        }
        for index, candidate in enumerate(ordered, start=1)
    ]


def _ambiguous_content_message(candidates: list[dict[str, Any]]) -> str:
    """Explain title ambiguity without weakening the confirmed content evidence."""
    names = [str(item.get("book_name") or "").strip() for item in candidates]
    names = list(dict.fromkeys(name for name in names if name))
    if len(names) == 1:
        title_text = f"《{names[0]}》等多个候选"
    elif len(names) == 2:
        title_text = f"《{names[0]}》与《{names[1]}》"
    else:
        title_text = "、".join(f"《{name}》" for name in names[:2]) + f"等 {len(names)} 部剧"
    return (
        f"内容已命中：{title_text}均存在大段连续台词复用，"
        "疑似同一内容的不同标题或版本，系统无法自动确认唯一剧名。"
    )


def _percent(value: float) -> str:
    return f"{max(0.0, min(value, 1.0)) * 100:.1f}%"


def _lexical_review_feedback(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = _metrics(candidate)
    shared = _shared_count(candidate)
    query_coverage = _query_coverage(candidate)
    evidence_coverage = _evidence_coverage(candidate)
    unit_label = str(metrics.get("match_unit_label") or "连续文本单元")
    semantic_score = candidate.get("semantic_score")
    summary = (
        f"检测到 {shared} 个{unit_label}重合；候选片段中有 {_percent(evidence_coverage)} 可在输入中直接找到，"
        f"但仅覆盖输入的 {_percent(query_coverage)}。"
    )
    signals = [
        {"label": unit_label, "value": f"{shared} 个"},
        {"label": "输入覆盖", "value": _percent(query_coverage)},
        {"label": "候选片段覆盖", "value": _percent(evidence_coverage)},
    ]
    if semantic_score is not None:
        signals.append({"label": "语义相似度", "value": f"{_semantic_score(candidate):.3f}"})
    return {
        "reason_type": "partial_continuous_dialogue_overlap",
        "title": "发现局部连续台词复用",
        "summary": summary,
        "recommended_action": "建议核对人物名称替换、关键剧情顺序及后续对白是否同源。",
        "signals": signals,
    }


def _semantic_review_feedback(candidate: dict[str, Any]) -> dict[str, Any]:
    score = _semantic_score(candidate)
    return {
        "reason_type": "semantic_similarity_without_sufficient_lexical_evidence",
        "title": "发现语义相近内容",
        "summary": f"语义相似度为 {score:.3f}，但没有发现足够的连续台词复用证据。",
        "recommended_action": "建议核对核心情节、角色关系和可直接对应的完整对白。",
        "signals": [{"label": "语义相似度", "value": f"{score:.3f}"}],
    }


def decide_drama_subtitle_match(
    *,
    candidates: list[dict[str, Any]],
    semantic_status: str,
    query_language_code: str = "",
    query_text: str = "",
) -> dict[str, Any]:
    """Convert internal retrieval candidates into a conservative public verdict."""
    normalized_candidates = [item for item in candidates if isinstance(item, dict)]
    strong_by_book: dict[str, list[dict[str, Any]]] = {}
    for candidate in normalized_candidates:
        book_id = str(candidate.get("book_id") or "")
        if book_id and (
            _is_strong_lexical(candidate)
            or _is_chinese_fuzzy_lexical_match(
                candidate,
                query_text=query_text,
                query_language_code=query_language_code,
            )
        ):
            strong_by_book.setdefault(book_id, []).append(candidate)

    # Make the operational verdict explicit. Evidence can be strong enough to
    # identify content while still requiring a human to resolve its title.
    def public_status(*, status: str, confirmed: bool) -> dict[str, Any]:
        return {
            "hit_status": MATCHED if confirmed else status,
            "is_confirmed_match": confirmed,
        }

    if len(strong_by_book) == 1:
        matched_candidates = next(iter(strong_by_book.values()))
        primary = max(matched_candidates, key=_candidate_sort_key)
        primary_book_id = str(primary.get("book_id") or "")
        related_episode_orders = sorted(
            {
                _integer(candidate.get("episode_order"))
                for candidate in normalized_candidates
                if str(candidate.get("book_id") or "") == primary_book_id
                and _integer(candidate.get("episode_order")) > 0
                and _integer(candidate.get("episode_order")) != _integer(primary.get("episode_order"))
                and _shared_count(candidate) >= 4
            }
        )
        fuzzy_confirmed = (
            not _is_strong_lexical(primary)
            and _is_chinese_fuzzy_lexical_match(
                primary,
                query_text=query_text,
                query_language_code=query_language_code,
            )
        )
        return {
            "matched": True,
            "status": MATCHED,
            **public_status(status=MATCHED, confirmed=True),
            "outcome": "confirmed_match",
            "content_match_status": "matched",
            "title_resolution": "unique",
            "reason": "strong_fuzzy_lexical_evidence" if fuzzy_confirmed else "strong_lexical_evidence",
            "user_message": (
                "已确认命中：检测文本与库内剧集存在充分的连续文本复用证据。"
                if not fuzzy_confirmed
                else "已确认命中：检测到大量同序文本复用，已通过中文转写容错对齐核验。"
            ),
            "fuzzy_alignment_score": round(_ordered_fuzzy_alignment(primary, query_text), 4) if fuzzy_confirmed else None,
            **_candidate_reference(primary),
            "related_episode_orders": related_episode_orders,
        }

    if len(strong_by_book) > 1:
        strongest = max((max(items, key=_candidate_sort_key) for items in strong_by_book.values()), key=_candidate_sort_key)
        strong_candidates = _strong_candidate_references(strong_by_book)
        return {
            "matched": False,
            "status": REVIEW_REQUIRED,
            **public_status(status=REVIEW_REQUIRED, confirmed=False),
            "outcome": "content_matched_ambiguous",
            "content_match_status": "matched",
            "title_resolution": "ambiguous",
            "reason": "multiple_books_with_strong_lexical_evidence",
            "user_message": _ambiguous_content_message(strong_candidates),
            "review_feedback": {
                "reason_type": "multiple_strong_content_versions",
                "title": "内容已命中，但剧名归属不唯一",
                "summary": f"库内有 {len(strong_candidates)} 部剧均存在强连续台词证据，无法安全指定唯一剧名。",
                "recommended_action": "请按强证据候选优先级核对标题、角色名和上架版本。",
                "signals": [{"label": "强证据候选", "value": f"{len(strong_candidates)} 部"}],
            },
            **_candidate_reference(strongest),
            "related_episode_orders": [],
            "strong_match_candidates": strong_candidates,
        }

    weak_lexical = [item for item in normalized_candidates if _is_weak_lexical(item)]
    if weak_lexical:
        candidate = max(weak_lexical, key=_candidate_sort_key)
        feedback = _lexical_review_feedback(candidate)
        return {
            "matched": False,
            "status": REVIEW_REQUIRED,
            **public_status(status=REVIEW_REQUIRED, confirmed=False),
            "outcome": "potential_match",
            "content_match_status": "uncertain",
            "title_resolution": "unresolved",
            "reason": "lexical_evidence_below_acceptance_threshold",
            "user_message": f"需复核：{feedback['summary']} 覆盖范围不足以自动确认命中。",
            "review_feedback": feedback,
            **_candidate_reference(candidate),
            "related_episode_orders": [],
        }

    semantic_review = [
        item
        for item in normalized_candidates
        if semantic_status == "ok"
        and _is_same_language(item, query_language_code)
        and _semantic_score(item) >= 0.82
    ]
    if semantic_review:
        candidate = max(semantic_review, key=_candidate_sort_key)
        feedback = _semantic_review_feedback(candidate)
        return {
            "matched": False,
            "status": REVIEW_REQUIRED,
            **public_status(status=REVIEW_REQUIRED, confirmed=False),
            "outcome": "potential_match",
            "content_match_status": "uncertain",
            "title_resolution": "unresolved",
            "reason": "semantic_candidate_requires_manual_review",
            "user_message": f"需复核：{feedback['summary']} 需要人工核验。",
            "review_feedback": feedback,
            **_candidate_reference(candidate),
            "related_episode_orders": [],
        }

    return {
        "matched": False,
        "status": NOT_MATCHED,
        **public_status(status=NOT_MATCHED, confirmed=False),
        "outcome": "no_match",
        "content_match_status": "not_matched",
        "title_resolution": "not_applicable",
        "reason": "semantic_unavailable_no_lexical_evidence" if semantic_status == "fallback_lexical_only" else "no_candidate_above_acceptance_threshold",
        "user_message": (
            "语义服务暂不可用，且没有可确认的词级复用证据。"
            if semantic_status == "fallback_lexical_only"
            else "未命中当前字幕库。"
        ),
        "related_episode_orders": [],
    }
