from __future__ import annotations

from typing import Any

from service.drama_subtitle_video_compare import (
    build_drama_subtitle_video_segments,
    compare_drama_subtitle_video,
)


def _cues() -> list[dict[str, object]]:
    return [
        {"start_seconds": 0, "end_seconds": 10, "text": "a" * 170},
        {"start_seconds": 10, "end_seconds": 20, "text": "b" * 170},
        {"start_seconds": 20, "end_seconds": 30, "text": "c" * 170},
    ]


def _payload(outcome: str) -> dict[str, Any]:
    mapping = {
        "confirmed_match": ("matched", "matched", "unique"),
        "content_matched_ambiguous": ("review_required", "matched", "ambiguous"),
        "potential_match": ("review_required", "uncertain", "unresolved"),
        "no_match": ("not_matched", "not_matched", "not_applicable"),
    }
    status, content_status, title_resolution = mapping[outcome]
    return {
        "decision": {
            "matched": outcome == "confirmed_match",
            "status": status,
            "outcome": outcome,
            "content_match_status": content_status,
            "title_resolution": title_resolution,
        },
        "candidates": [],
        "semantic_status": "ok",
    }


def test_video_segments_are_cue_aligned_and_overlap_once():
    segments = build_drama_subtitle_video_segments(_cues())

    assert len(segments) == 2
    assert segments[0]["cue_start_order"] == 1
    assert segments[0]["cue_end_order"] == 2
    assert segments[1]["cue_start_order"] == 2
    assert segments[1]["cue_end_order"] == 3
    assert segments[0]["source_time_start"] == 0
    assert segments[1]["source_time_end"] == 30


def test_video_compare_stops_after_definitive_fast_screen():
    calls: list[str] = []

    def search(**kwargs: object) -> dict[str, Any]:
        calls.append(str(kwargs["query_text"]))
        return _payload("confirmed_match")

    result = compare_drama_subtitle_video(
        db_path="unused.sqlite3",
        query_text="full subtitle",
        cues=_cues(),
        semantic_enabled=False,
        search_function=search,
    )

    assert calls == ["full subtitle"]
    assert result["video_decision"]["outcome"] == "confirmed_match"
    assert result["execution"]["strategy"] == "fast_screen_only"
    assert result["execution"]["processed_segment_count"] == 0


def test_video_compare_falls_back_and_stops_after_confirmed_segment():
    calls: list[str] = []
    outcomes = iter(["no_match", "no_match", "content_matched_ambiguous"])

    def search(**kwargs: object) -> dict[str, Any]:
        calls.append(str(kwargs["query_text"]))
        return _payload(next(outcomes))

    result = compare_drama_subtitle_video(
        db_path="unused.sqlite3",
        query_text="full subtitle",
        cues=_cues(),
        semantic_enabled=False,
        search_function=search,
    )

    assert len(calls) == 3
    assert result["execution"]["fallback_triggered"] is True
    assert result["execution"]["segment_count"] == 2
    assert result["execution"]["processed_segment_count"] == 2
    assert result["video_decision"]["outcome"] == "content_matched_ambiguous"
    assert result["video_decision"]["matched_segment_order"] == 2
