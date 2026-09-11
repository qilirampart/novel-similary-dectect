from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from service.drama_subtitle_hybrid_retrieval import (
    DEFAULT_STRONG_LEXICAL_COVERAGE,
    search_drama_subtitle_hybrid_candidates,
)
from service.drama_subtitle_semantic_retrieval import DramaSubtitleSemanticConfig


DEFAULT_MIN_SEGMENT_CHARS = 320
DEFAULT_MAX_SEGMENT_CHARS = 500

SearchFunction = Callable[..., dict[str, Any]]


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cue_text(cue: Mapping[str, object]) -> str:
    return str(cue.get("text") or "").strip()


def _normalize_cues(raw_cues: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    cues: list[dict[str, object]] = []
    for index, raw_cue in enumerate(raw_cues, start=1):
        text = _cue_text(raw_cue)
        if not text:
            continue
        start = max(0.0, _number(raw_cue.get("start_seconds")))
        end = max(start, _number(raw_cue.get("end_seconds"), start))
        cues.append(
            {
                "cue_order": index,
                "text": text,
                "start_seconds": start,
                "end_seconds": end,
            }
        )
    return cues


def build_drama_subtitle_video_segments(
    raw_cues: Sequence[Mapping[str, object]],
    *,
    min_chars: int = DEFAULT_MIN_SEGMENT_CHARS,
    max_chars: int = DEFAULT_MAX_SEGMENT_CHARS,
) -> list[dict[str, object]]:
    """Build complete, cue-aligned segments with one cue of overlap."""
    if min_chars <= 0 or max_chars < min_chars:
        raise ValueError("segment character limits are invalid")
    cues = _normalize_cues(raw_cues)
    if not cues:
        raise ValueError("at least one non-empty subtitle cue is required")

    segments: list[dict[str, object]] = []
    start_index = 0
    while start_index < len(cues):
        selected: list[dict[str, object]] = []
        char_count = 0
        index = start_index
        while index < len(cues):
            cue = cues[index]
            next_length = len(str(cue["text"]))
            if selected and char_count >= min_chars and char_count + next_length > max_chars:
                break
            selected.append(cue)
            char_count += next_length
            index += 1
            if char_count >= min_chars and (index >= len(cues) or char_count + len(str(cues[index]["text"])) > max_chars):
                break

        if not selected:
            break
        segments.append(
            {
                "segment_order": len(segments) + 1,
                "cue_start_order": selected[0]["cue_order"],
                "cue_end_order": selected[-1]["cue_order"],
                "source_time_start": selected[0]["start_seconds"],
                "source_time_end": selected[-1]["end_seconds"],
                "query_text": "\n".join(str(cue["text"]) for cue in selected),
                "query_char_count": char_count,
            }
        )
        if index >= len(cues):
            break
        # Reuse the final cue to avoid losing a phrase that crosses a boundary.
        start_index = max(start_index + 1, index - 1)
    return segments


def _decision(payload: Mapping[str, object]) -> dict[str, object]:
    value = payload.get("decision")
    return dict(value) if isinstance(value, Mapping) else {}


def _is_definitive(decision: Mapping[str, object]) -> bool:
    return (
        str(decision.get("content_match_status") or "") == "matched"
        and str(decision.get("title_resolution") or "") in {"unique", "ambiguous"}
    )


def _video_decision(
    decision: Mapping[str, object],
    *,
    source: str,
    segment_order: int | None,
    video_reason: str,
) -> dict[str, object]:
    result = dict(decision)
    result["decision_scope"] = "video"
    result["decision_source"] = source
    result["matched_segment_order"] = segment_order
    result["video_reason"] = video_reason
    return result


def _segment_result(segment: Mapping[str, object], payload: Mapping[str, object], duration_seconds: float) -> dict[str, object]:
    return {
        "segment_order": segment["segment_order"],
        "cue_start_order": segment["cue_start_order"],
        "cue_end_order": segment["cue_end_order"],
        "source_time_start": segment["source_time_start"],
        "source_time_end": segment["source_time_end"],
        "query_char_count": segment["query_char_count"],
        "duration_seconds": round(duration_seconds, 4),
        "decision": _decision(payload),
        "semantic_status": payload.get("semantic_status"),
        "semantic_error": payload.get("semantic_error"),
        "candidates": payload.get("candidates") or [],
    }


def compare_drama_subtitle_video(
    *,
    db_path: str | Path,
    query_text: str,
    cues: Sequence[Mapping[str, object]],
    candidate_limit: int = 10,
    window_limit: int = 200,
    language_code: str = "",
    semantic_enabled: bool = True,
    semantic_config: DramaSubtitleSemanticConfig = DramaSubtitleSemanticConfig(),
    semantic_window_limit: int = 100,
    strong_lexical_coverage: float = DEFAULT_STRONG_LEXICAL_COVERAGE,
    search_function: SearchFunction = search_drama_subtitle_hybrid_candidates,
) -> dict[str, object]:
    """Run one fast video screen, then cue-aligned fallback only when needed."""
    normalized_query = str(query_text or "").strip()
    if not normalized_query:
        raise ValueError("query_text is empty")

    def search(text: str) -> dict[str, Any]:
        return search_function(
            db_path=str(db_path),
            query_text=text,
            candidate_limit=candidate_limit,
            window_limit=window_limit,
            include_window_text=True,
            language_code=language_code,
            semantic_enabled=semantic_enabled,
            semantic_config=semantic_config,
            semantic_window_limit=semantic_window_limit,
            strong_lexical_coverage=strong_lexical_coverage,
        )

    fast_started_at = time.perf_counter()
    fast_payload = search(normalized_query)
    fast_duration = time.perf_counter() - fast_started_at
    fast_decision = _decision(fast_payload)
    if _is_definitive(fast_decision):
        return {
            "video_decision": _video_decision(
                fast_decision,
                source="fast_screen",
                segment_order=None,
                video_reason="fast_screen_definitive",
            ),
            "execution": {
                "strategy": "fast_screen_only",
                "fast_screen_duration_seconds": round(fast_duration, 4),
                "segment_count": 0,
                "processed_segment_count": 0,
                "stopped_early": False,
                "fallback_triggered": False,
            },
            "fast_screen": fast_payload,
            "segments": [],
        }

    segments = build_drama_subtitle_video_segments(cues)
    segment_results: list[dict[str, object]] = []
    possible_decisions: list[tuple[dict[str, object], str, int | None]] = []
    if str(fast_decision.get("outcome") or "") == "potential_match":
        possible_decisions.append((fast_decision, "fast_screen", None))

    definitive_decision: dict[str, object] | None = None
    definitive_order: int | None = None
    for segment in segments:
        segment_started_at = time.perf_counter()
        payload = search(str(segment["query_text"]))
        segment_result = _segment_result(segment, payload, time.perf_counter() - segment_started_at)
        segment_results.append(segment_result)
        decision = _decision(payload)
        if _is_definitive(decision):
            definitive_decision = decision
            definitive_order = int(segment["segment_order"])
            break
        if str(decision.get("outcome") or "") == "potential_match":
            possible_decisions.append((decision, "segment", int(segment["segment_order"])))

    stopped_early = definitive_order is not None and definitive_order < len(segments)
    if definitive_decision is not None:
        final_decision = _video_decision(
            definitive_decision,
            source="segment",
            segment_order=definitive_order,
            video_reason="segment_definitive",
        )
    elif possible_decisions:
        selected, source, order = possible_decisions[0]
        final_decision = _video_decision(
            selected,
            source=source,
            segment_order=order,
            video_reason="segment_exhausted_with_potential_match",
        )
    else:
        final_decision = _video_decision(
            fast_decision,
            source="fast_screen",
            segment_order=None,
            video_reason="segment_exhausted_without_match",
        )

    return {
        "video_decision": final_decision,
        "execution": {
            "strategy": "fast_screen_then_segment_fallback",
            "fast_screen_duration_seconds": round(fast_duration, 4),
            "segment_count": len(segments),
            "processed_segment_count": len(segment_results),
            "stopped_early": stopped_early,
            "fallback_triggered": True,
        },
        "fast_screen": fast_payload,
        "segments": segment_results,
    }
