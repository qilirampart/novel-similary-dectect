from __future__ import annotations

import json
import threading
import time

from service.business_store import init_business_db
from service.drama_subtitle_translation import (
    _build_translation_assisted_result,
    DramaSubtitleTranslationConfig,
    TENCENT_PROVIDER_KEY,
    TENCENT_TMT_ENDPOINT,
    _select_translation_review,
    apply_translation_fallback,
    target_languages_for,
)


def _native_payload(outcome: str = "no_match") -> dict:
    is_confirmed = outcome == "confirmed_match"
    return {
        "query_text": "原始字幕",
        "query_language_code": "zh",
        "decision": {
            "matched": is_confirmed,
            "status": "matched" if is_confirmed else "not_matched",
            "outcome": outcome,
            "content_match_status": "matched" if is_confirmed else "not_matched",
            "title_resolution": "unique" if is_confirmed else "not_applicable",
        },
        "candidates": [],
    }


def _translated_payload(**_: object) -> dict:
    return {
        "query_language_code": "en",
        "candidate_count": 1,
        "candidates": [{"rank": 1, "book_id": "book-1", "book_name": "Target Drama"}],
        "decision": {
            "matched": True,
            "status": "matched",
            "outcome": "confirmed_match",
            "content_match_status": "matched",
            "title_resolution": "unique",
            "book_id": "book-1",
            "book_name": "Target Drama",
            "candidate_rank": 1,
        },
    }


def test_translation_fallback_uses_private_cache_and_never_auto_confirms(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    calls: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"translations": [{"text": "translated subtitle"}]}).encode("utf-8")

    def fake_urlopen(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return FakeResponse()

    monkeypatch.setattr("service.drama_subtitle_translation.urlopen", fake_urlopen)
    config = DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key")
    first = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="原始字幕",
        search_function=_translated_payload,
        search_kwargs={},
        config=config,
        business_db_path=str(db_path),
        owner_user_id=1,
    )
    second = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="原始字幕",
        search_function=_translated_payload,
        search_kwargs={},
        config=config,
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert first["decision"]["outcome"] == "translation_assisted_match"
    assert first["decision"]["matched"] is False
    assert first["decision"]["status"] == "review_required"
    assert first["decision"]["hit_status"] == "review_required"
    assert first["decision"]["is_confirmed_match"] is False
    assert first["translation_fallback"]["match_status"] == "review_required"
    assert first["translation_fallback"]["attempts"][0]["cache_hit"] is False
    assert second["translation_fallback"]["attempts"][0]["cache_hit"] is True
    assert len(calls) == 1


def test_translation_fallback_does_not_run_after_native_match(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("translation must not run")

    monkeypatch.setattr("service.drama_subtitle_translation.urlopen", fail_urlopen)
    result = apply_translation_fallback(
        native_payload=_native_payload("confirmed_match"),
        query_text="原始字幕",
        search_function=_translated_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert result["translation_fallback"]["status"] == "not_needed"


def test_translation_fallback_runs_after_native_potential_match(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"translations": [{"text": "translated subtitle"}]}).encode("utf-8")

    monkeypatch.setattr("service.drama_subtitle_translation.urlopen", lambda *_args, **_kwargs: FakeResponse())
    native = _native_payload("potential_match")
    native["decision"].update({"status": "review_required", "content_match_status": "uncertain", "title_resolution": "unresolved"})
    result = apply_translation_fallback(
        native_payload=native,
        query_text="原始字幕",
        search_function=_translated_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert result["decision"]["outcome"] == "translation_assisted_match"


def test_translation_fallback_stops_after_total_budget(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    translated_targets: list[str] = []

    def slow_translate(**kwargs: object) -> dict[str, object]:
        translated_targets.append(str(kwargs["target_language_code"]))
        time.sleep(0.11)
        return {
            "text": "translated subtitle",
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.11,
        }

    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_deepl",
        slow_translate,
    )
    result = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="source subtitle",
        search_function=lambda **_kwargs: {
            "candidate_count": 0,
            "decision": {"outcome": "no_match"},
        },
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(
            enabled=True,
            deepl_api_key="test-key",
            timeout_seconds=1,
            total_timeout_seconds=0.1,
        ),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert translated_targets == ["en"]
    assert result["translation_fallback"]["status"] == "budget_exhausted"


def test_translation_targets_cover_all_other_corpus_languages():
    assert target_languages_for("ja") == ("zh", "ko", "en")
    assert target_languages_for("pt")[0] == "en"
    assert set(target_languages_for("pt")) == {"zh", "en", "ko", "ja"}


def test_translation_fallback_runs_target_languages_with_bounded_parallelism(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    active_calls = 0
    max_active_calls = 0
    active_lock = threading.Lock()
    translated_targets: list[str] = []

    monkeypatch.setattr(
        "service.drama_subtitle_translation.target_languages_for",
        lambda _source: ("zh", "ko", "en"),
    )

    def slow_translate(**kwargs: object) -> dict[str, object]:
        nonlocal active_calls, max_active_calls
        target = str(kwargs["target_language_code"])
        with active_lock:
            translated_targets.append(target)
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
        try:
            time.sleep(0.04)
        finally:
            with active_lock:
                active_calls -= 1
        return {
            "text": f"translated {target}",
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.04,
        }

    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        slow_translate,
    )

    result = apply_translation_fallback(
        native_payload={"query_text": "原字幕", "query_language_code": "ja", "decision": {"outcome": "no_match"}},
        query_text="原字幕",
        search_function=lambda **_kwargs: {
            "candidate_count": 0,
            "candidates": [],
            "decision": {"outcome": "no_match", "content_match_status": "not_matched"},
        },
        search_kwargs={"semantic_enabled": False},
        config=DramaSubtitleTranslationConfig(
            enabled=True,
            deepl_api_key="test-key",
            target_parallelism=2,
        ),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert set(translated_targets) == {"zh", "ko", "en"}
    assert max_active_calls == 2
    assert [item["target_language_code"] for item in result["translation_fallback"]["attempts"]] == [
        "zh",
        "ko",
        "en",
    ]
    assert result["translation_fallback"]["target_parallelism"] == 2


def test_translation_fallback_overlaps_slow_primary_target_before_it_finishes(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    primary_finished = threading.Event()
    overlapped_targets: list[str] = []

    monkeypatch.setattr(
        "service.drama_subtitle_translation.target_languages_for",
        lambda _source: ("zh", "ko", "en"),
    )

    def staged_translate(**kwargs: object) -> dict[str, object]:
        target = str(kwargs["target_language_code"])
        if target == "zh":
            time.sleep(0.06)
            primary_finished.set()
        else:
            if not primary_finished.is_set():
                overlapped_targets.append(target)
            time.sleep(0.005)
        return {
            "text": f"translated {target}",
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        staged_translate,
    )

    result = apply_translation_fallback(
        native_payload={"query_text": "原字幕", "query_language_code": "ja", "decision": {"outcome": "no_match"}},
        query_text="原字幕",
        search_function=lambda **_kwargs: {
            "candidate_count": 0,
            "candidates": [],
            "decision": {"outcome": "no_match", "content_match_status": "not_matched"},
        },
        search_kwargs={"semantic_enabled": False},
        config=DramaSubtitleTranslationConfig(
            enabled=True,
            deepl_api_key="test-key",
            target_parallelism=2,
            primary_target_head_start_seconds=0.005,
        ),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert overlapped_targets
    assert [item["target_language_code"] for item in result["translation_fallback"]["attempts"]] == [
        "zh",
        "ko",
        "en",
    ]


def test_translation_semantic_candidate_is_promoted_to_review_and_traced(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)

    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **kwargs: {
            "text": f"translated to {kwargs['target_language_code']}",
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.01,
        },
    )

    def semantic_payload(**kwargs: object) -> dict:
        target = str(kwargs["language_code"])
        if target != "ko":
            return {"candidate_count": 0, "candidates": [], "decision": {"outcome": "no_match"}}
        return {
            "candidate_count": 3,
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "correct-book",
                    "book_name": "Correct Drama",
                    "episode_order": 1,
                    "language_code": "ko",
                    "semantic_score": 0.68,
                    "retrieval_sources": ["semantic"],
                    "evidence": {"window_uid": "correct-book:1:1-16", "window_text": "correct evidence"},
                },
                {
                    "rank": 2,
                    "book_id": "correct-book",
                    "book_name": "Correct Drama",
                    "episode_order": 2,
                    "language_code": "ko",
                    "semantic_score": 0.64,
                    "retrieval_sources": ["semantic"],
                    "evidence": {"window_uid": "correct-book:2:1-16", "window_text": "more evidence"},
                },
                {
                    "rank": 3,
                    "book_id": "noise-book",
                    "book_name": "Noise Drama",
                    "episode_order": 1,
                    "language_code": "ko",
                    "semantic_score": 0.61,
                    "retrieval_sources": ["semantic"],
                    "evidence": {"window_uid": "noise-book:1:1-16", "window_text": "noise"},
                },
            ],
            "decision": {"outcome": "no_match"},
        }

    native = _native_payload()
    native["query_language_code"] = "ja"
    result = apply_translation_fallback(
        native_payload=native,
        query_text="日本語字幕",
        search_function=semantic_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(
            enabled=True,
            deepl_api_key="test-key",
            total_timeout_seconds=10,
        ),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert result["decision"]["status"] == "review_required"
    assert result["decision"]["outcome"] == "translation_assisted_match"
    assert result["decision"]["book_id"] == "correct-book"
    assert result["query_language_code"] == "ja"
    assert result["translation_target_language_code"] == "ko"
    assert result["translation_fallback"]["matched_target_language_code"] == "ko"
    ko_attempt = next(
        item for item in result["translation_fallback"]["attempts"]
        if item["target_language_code"] == "ko"
    )
    assert ko_attempt["candidates"][0]["evidence_text"] == "correct evidence"
    assert ko_attempt["decision"]["reason"] == "translation_semantic_candidate_requires_review"


def test_translation_review_prefers_repeated_book_support_over_one_higher_score(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)

    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **kwargs: {
            "text": f"translated to {kwargs['target_language_code']}",
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.01,
        },
    )

    def candidate(book_id: str, score: float, rank: int, episode: int) -> dict:
        return {
            "rank": rank,
            "book_id": book_id,
            "book_name": book_id,
            "episode_order": episode,
            "language_code": "en",
            "semantic_score": score,
            "retrieval_sources": ["semantic"],
            "evidence": {"window_uid": f"{book_id}:{episode}", "window_text": "evidence"},
        }

    def semantic_payload(**kwargs: object) -> dict:
        target = str(kwargs["language_code"])
        if target == "zh":
            candidates = [
                candidate("correct-book", 0.68, 1, 1),
                candidate("correct-book", 0.65, 2, 2),
                candidate("correct-book", 0.63, 3, 3),
                candidate("zh-noise", 0.64, 4, 1),
            ]
        elif target == "en":
            candidates = [
                candidate("wrong-high-book", 0.79, 1, 1),
                candidate("wrong-high-book", 0.75, 2, 2),
                candidate("en-noise", 0.70, 3, 1),
            ]
        else:
            candidates = []
        return {
            "candidate_count": len(candidates),
            "candidates": candidates,
            "decision": {"outcome": "no_match"},
        }

    native = _native_payload()
    native["query_language_code"] = "ja"
    result = apply_translation_fallback(
        native_payload=native,
        query_text="日本語字幕",
        search_function=semantic_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert result["decision"]["book_id"] == "correct-book"
    assert result["translation_fallback"]["matched_target_language_code"] == "zh"


def test_translation_review_prefers_continuous_alignment_across_target_languages(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)

    translated_texts = {
        "zh": "这是一段关于家族公司和继承权的普通对白，没有目标剧情中的连续事件。",
        "ko": "회사와 가족에 관한 일반적인 대화이며 실제 목표 줄거리와는 다릅니다.",
        "en": (
            "We promised to attend the same university. You entered my apartment without permission, "
            "borrowed my suit, and used the door code that belonged only to the three of us."
        ),
    }
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **kwargs: {
            "text": translated_texts[kwargs["target_language_code"]],
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.01,
        },
    )

    def candidate(book_id: str, score: float, rank: int, episode: int, evidence: str) -> dict:
        return {
            "rank": rank,
            "book_id": book_id,
            "book_name": book_id,
            "episode_order": episode,
            "language_code": "en",
            "semantic_score": score,
            "retrieval_sources": ["semantic"],
            "evidence": {"window_uid": f"{book_id}:{episode}", "window_text": evidence},
        }

    def semantic_payload(**kwargs: object) -> dict:
        target = str(kwargs["language_code"])
        if target == "zh":
            candidates = [
                candidate("wrong-repeated", 0.69, 1, 1, "公司正在召开会议，继承人需要处理家族内部的普通争议。"),
                candidate("wrong-repeated", 0.66, 2, 2, "董事会要求负责人尽快解决公司资金和家庭问题。"),
            ]
        elif target == "en":
            candidates = [
                candidate(
                    "correct-content",
                    0.72,
                    1,
                    1,
                    (
                        "You entered my apartment without permission. You borrowed my suit and used the door code "
                        "that belonged only to the three of us. I never said you could take my clothes."
                    ),
                )
            ]
        else:
            candidates = []
        return {
            "candidate_count": len(candidates),
            "candidates": candidates,
            "decision": {"outcome": "no_match", "content_match_status": "not_matched"},
        }

    native = _native_payload()
    native["query_language_code"] = "ja"
    result = apply_translation_fallback(
        native_payload=native,
        query_text="日本語字幕",
        search_function=semantic_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert result["decision"]["book_id"] == "correct-content"
    assert result["decision"]["outcome"] == "confirmed_match"
    assert result["decision"]["ordered_alignment_score"] >= 0.38
    assert result["decision"]["matched"] is True
    assert result["decision"]["status"] == "matched"
    assert result["decision"]["content_match_status"] == "matched"
    assert result["decision"]["translation_alignment_auto_confirmed"] is True
    assert result["translation_fallback"]["matched_target_language_code"] == "en"
    assert result["decision"]["content_candidate_options"][0]["book_id"] == "correct-content"
    assert result["decision"]["content_candidate_options"][0]["review_priority"] == 1


def test_translation_strong_alignment_with_competing_books_stays_ambiguous(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    translated_text = (
        "The heir entered the office, found the hidden contract, and confronted the secretary "
        "before the family meeting began."
    )
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **kwargs: {
            "text": translated_text,
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.01,
        },
    )

    def semantic_payload(**kwargs: object) -> dict:
        if str(kwargs["language_code"]) != "en":
            return {"candidate_count": 0, "candidates": [], "decision": {"outcome": "no_match"}}
        candidates = []
        for rank, book_id in ((1, "book-a"), (2, "book-b")):
            candidates.append(
                {
                    "rank": rank,
                    "book_id": book_id,
                    "book_name": book_id,
                    "episode_order": 1,
                    "language_code": "en",
                    "semantic_score": 0.74 - (rank - 1) * 0.01,
                    "retrieval_sources": ["semantic"],
                    "evidence": {
                        "window_uid": f"{book_id}:1",
                        "window_text": f"Opening line. {translated_text} Closing line.",
                    },
                }
            )
        return {
            "candidate_count": len(candidates),
            "candidates": candidates,
            "decision": {"outcome": "no_match", "content_match_status": "not_matched"},
        }

    native = _native_payload()
    native["query_language_code"] = "ja"
    result = apply_translation_fallback(
        native_payload=native,
        query_text="日本語字幕",
        search_function=semantic_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    decision = result["decision"]
    assert decision["status"] == "review_required"
    assert decision["hit_status"] == "review_required"
    assert decision["is_confirmed_match"] is False
    assert decision["outcome"] == "content_matched_ambiguous"
    assert decision["content_match_status"] == "matched"
    assert decision["title_resolution"] == "ambiguous"
    assert decision["translation_alignment_book_ids"] == ["book-a", "book-b"]


def test_alignment_review_does_not_replace_existing_legacy_candidate():
    def review(book_id: str, reason: str, alignment_score: float, support_count: int = 0) -> dict:
        return {
            "translated_payload": {},
            "translated": {},
            "target_language": "en" if "alignment" in reason else "ko",
            "translated_decision": {
                "book_id": book_id,
                "reason": reason,
                "ordered_alignment_score": alignment_score,
                "same_book_candidate_count": support_count,
                "semantic_score": 0.70,
            },
        }

    legacy = review("stable-legacy", "translation_semantic_candidate_requires_review", 0.28, 3)
    marginal_alignment = review("marginal-alternative", "translation_continuous_alignment_requires_review", 0.42)
    clear_alignment = review("clear-alternative", "translation_continuous_alignment_requires_review", 0.45)
    no_evidence_legacy = review("no-evidence-legacy", "translation_semantic_candidate_requires_review", 0.10, 1)

    assert _select_translation_review([legacy, marginal_alignment])["translated_decision"]["book_id"] == "stable-legacy"
    assert _select_translation_review([legacy, clear_alignment])["translated_decision"]["book_id"] == "stable-legacy"
    assert _select_translation_review([no_evidence_legacy, clear_alignment])["translated_decision"]["book_id"] == "clear-alternative"


def test_high_confidence_alignment_replaces_moderate_legacy_candidate():
    legacy = {
        "translated_payload": {},
        "translated": {},
        "target_language": "zh",
        "translated_decision": {
            "book_id": "legacy-book",
            "reason": "translation_semantic_candidate_requires_review",
            "ordered_alignment_score": 0.31,
            "semantic_score": 0.65,
        },
    }
    alignment = {
        "translated_payload": {},
        "translated": {},
        "target_language": "en",
        "translated_decision": {
            "book_id": "aligned-book",
            "reason": "translation_continuous_alignment_requires_review",
            "ordered_alignment_score": 0.84,
            "aligned_character_count": 180,
            "evidence_character_count": 250,
            "semantic_score": 0.67,
        },
    }

    assert _select_translation_review([legacy, alignment])["translated_decision"]["book_id"] == "aligned-book"


def test_translation_alignment_review_rejects_short_or_weak_overlap(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **kwargs: {
            "text": "pregnancy family hospital marriage",
            "cache_hit": False,
            "provider": "test",
            "duration_seconds": 0.01,
        },
    )

    def semantic_payload(**_kwargs: object) -> dict:
        return {
            "candidate_count": 1,
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "generic-drama",
                    "book_name": "Generic Drama",
                    "episode_order": 1,
                    "language_code": "en",
                    "semantic_score": 0.59,
                    "retrieval_sources": ["semantic"],
                    "evidence": {"window_uid": "generic:1", "window_text": "pregnancy and family"},
                }
            ],
            "decision": {"outcome": "no_match", "content_match_status": "not_matched"},
        }

    result = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="源字幕",
        search_function=semantic_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert result["decision"]["outcome"] == "no_match"


def test_translation_alignment_short_block_is_not_exposed_as_content_match():
    translated_text = "a" * 36
    evidence_text = f"prefix {translated_text} {'z' * 37}"
    result = _build_translation_assisted_result(
        native_payload={"query_text": "source", "query_language_code": "ja"},
        translated_payload={
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "short-book",
                    "book_name": "Short Evidence",
                    "episode_order": 1,
                    "language_code": "en",
                    "semantic_score": 0.75,
                    "evidence_text": evidence_text,
                }
            ]
        },
        translated_decision={
            "reason": "translation_continuous_alignment_requires_review",
            "book_id": "short-book",
            "book_name": "Short Evidence",
            "semantic_score": 0.75,
            "ordered_alignment_score": 0.4444,
            "aligned_character_count": 36,
            "evidence_character_count": 81,
        },
        translated={"text": translated_text, "provider": "test"},
        fallback={},
        source_language="ja",
        target_language="en",
    )

    assert result["decision"]["status"] == "review_required"
    assert result["decision"]["content_match_status"] == "uncertain"
    assert result["decision"]["is_confirmed_match"] is False


def test_translation_fallback_skips_semantic_when_lexical_precheck_is_already_strong(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    search_calls: list[dict[str, object]] = []
    translated_text = (
        "The shattered pendant, the emperor hearing her thoughts, and the seven-day warning "
        "all happened in exactly that order."
    )

    monkeypatch.setattr(
        "service.drama_subtitle_translation.target_languages_for",
        lambda _source: ("en",),
    )
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **_kwargs: {
            "text": translated_text,
            "cache_hit": True,
            "provider": "test",
            "duration_seconds": 0.0,
        },
    )

    def lexical_only_hit(**kwargs: object) -> dict[str, object]:
        search_calls.append(
            {
                "language_code": kwargs.get("language_code"),
                "semantic_enabled": kwargs.get("semantic_enabled"),
                "lexical_reused": "precomputed_lexical" in kwargs,
            }
        )
        if kwargs.get("semantic_enabled") is not False:
            raise AssertionError("semantic search should be skipped after a strong lexical precheck")
        return {
            "candidate_count": 1,
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "book-1",
                    "book_name": "Target Drama",
                    "episode_order": 1,
                    "language_code": "en",
                    "retrieval_sources": ["lexical"],
                    "match_metrics": {
                        "match_unit": "word_4gram",
                        "match_unit_label": "共享四词短语",
                        "shared_trigram_count": 16,
                        "query_trigram_count": 120,
                        "query_coverage_rate": 0.12,
                        "retrieved_window_query_coverage_rate": 0.12,
                        "evidence_coverage_rate": 0.84,
                    },
                    "evidence": {
                        "window_uid": "book-1:1:1-16",
                        "window_text": translated_text,
                    },
                }
            ],
            "decision": {
                "matched": True,
                "status": "matched",
                "outcome": "confirmed_match",
                "content_match_status": "matched",
                "title_resolution": "unique",
                "reason": "strong_lexical_evidence",
                "book_id": "book-1",
                "book_name": "Target Drama",
                "candidate_rank": 1,
                "matched_episode_order": 1,
                "language_code": "en",
            },
        }

    result = apply_translation_fallback(
        native_payload={"query_text": "原字幕", "query_language_code": "ja", "decision": {"outcome": "no_match"}},
        query_text="原字幕",
        search_function=lexical_only_hit,
        search_kwargs={"semantic_enabled": True},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert search_calls == [{"language_code": "en", "semantic_enabled": False, "lexical_reused": False}]
    assert result["translation_fallback"]["attempts"][0]["search_strategy"] == "lexical_only"
    assert result["decision"]["status"] == "review_required"
    assert result["decision"]["book_id"] == "book-1"


def test_translation_fallback_runs_semantic_after_empty_lexical_precheck(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    search_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "service.drama_subtitle_translation.target_languages_for",
        lambda _source: ("en",),
    )
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **_kwargs: {
            "text": "translated subtitle",
            "cache_hit": True,
            "provider": "test",
            "duration_seconds": 0.0,
        },
    )

    def staged_search(**kwargs: object) -> dict[str, object]:
        search_calls.append(
            {
                "language_code": kwargs.get("language_code"),
                "semantic_enabled": kwargs.get("semantic_enabled"),
                "lexical_reused": "precomputed_lexical" in kwargs,
            }
        )
        if kwargs.get("semantic_enabled") is False:
            return {
                "candidate_count": 0,
                "candidates": [],
                "decision": {
                    "matched": False,
                    "status": "not_matched",
                    "outcome": "no_match",
                    "content_match_status": "not_matched",
                    "title_resolution": "not_applicable",
                },
            }
        return {
            "candidate_count": 2,
            "lexical_reused": bool(kwargs.get("precomputed_lexical")),
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "correct-book",
                    "book_name": "Correct Drama",
                    "episode_order": 1,
                    "language_code": "en",
                    "semantic_score": 0.69,
                    "retrieval_sources": ["semantic"],
                    "evidence": {"window_uid": "correct-book:1:1-16", "window_text": "correct evidence"},
                },
                {
                    "rank": 2,
                    "book_id": "correct-book",
                    "book_name": "Correct Drama",
                    "episode_order": 2,
                    "language_code": "en",
                    "semantic_score": 0.64,
                    "retrieval_sources": ["semantic"],
                    "evidence": {"window_uid": "correct-book:2:1-16", "window_text": "more evidence"},
                },
            ],
            "decision": {
                "matched": False,
                "status": "not_matched",
                "outcome": "no_match",
                "content_match_status": "not_matched",
                "title_resolution": "not_applicable",
            },
        }

    result = apply_translation_fallback(
        native_payload={"query_text": "原字幕", "query_language_code": "ja", "decision": {"outcome": "no_match"}},
        query_text="原字幕",
        search_function=staged_search,
        search_kwargs={"semantic_enabled": True},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert search_calls == [
        {"language_code": "en", "semantic_enabled": False, "lexical_reused": False},
        {"language_code": "en", "semantic_enabled": True, "lexical_reused": True},
    ]
    assert result["translation_fallback"]["attempts"][0]["search_strategy"] == "lexical_then_hybrid"
    assert result["translation_fallback"]["attempts"][0]["hybrid_lexical_reused"] is True
    assert result["decision"]["status"] == "review_required"
    assert result["decision"]["book_id"] == "correct-book"


def test_translation_fallback_skips_semantic_for_high_signal_lexical_precheck(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    search_calls: list[dict[str, object]] = []
    translated_text = "The hidden contract, the fake heiress, and the family banquet all lined up in sequence."

    monkeypatch.setattr(
        "service.drama_subtitle_translation.target_languages_for",
        lambda _source: ("en",),
    )
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **_kwargs: {
            "text": translated_text,
            "cache_hit": True,
            "provider": "test",
            "duration_seconds": 0.0,
        },
    )

    def high_signal_lexical(**kwargs: object) -> dict[str, object]:
        search_calls.append(
            {
                "language_code": kwargs.get("language_code"),
                "semantic_enabled": kwargs.get("semantic_enabled"),
            }
        )
        if kwargs.get("semantic_enabled") is not False:
            raise AssertionError("semantic search should be skipped for a high-signal lexical precheck")
        return {
            "candidate_count": 1,
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "book-lexical",
                    "book_name": "Lexical Signal Drama",
                    "episode_order": 1,
                    "language_code": "en",
                    "semantic_score": 0.76,
                    "retrieval_sources": ["lexical"],
                    "match_metrics": {
                        "match_unit": "word_4gram",
                        "match_unit_label": "共享四词短语",
                        "shared_trigram_count": 9,
                        "query_trigram_count": 180,
                        "query_coverage_rate": 0.02,
                        "retrieved_window_query_coverage_rate": 0.02,
                        "evidence_coverage_rate": 0.09,
                    },
                    "evidence": {
                        "window_uid": "book-lexical:1:1-16",
                        "window_text": translated_text,
                    },
                }
            ],
            "decision": {
                "matched": False,
                "status": "not_matched",
                "outcome": "no_match",
                "content_match_status": "not_matched",
                "title_resolution": "not_applicable",
            },
        }

    result = apply_translation_fallback(
        native_payload={"query_text": "原字幕", "query_language_code": "ja", "decision": {"outcome": "no_match"}},
        query_text="原字幕",
        search_function=high_signal_lexical,
        search_kwargs={"semantic_enabled": True},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert search_calls == [{"language_code": "en", "semantic_enabled": False}]
    assert result["translation_fallback"]["attempts"][0]["search_strategy"] == "lexical_only"
    assert result["decision"]["status"] == "review_required"
    assert result["decision"]["book_id"] == "book-lexical"


def test_translation_fallback_keeps_strong_lexical_dialogue_as_review_without_semantic_score(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    translated_text = (
        "The hidden contract was signed before the family banquet. "
        "You took my inheritance, lied to my father, and locked me out of my own house."
    )
    search_calls: list[bool] = []

    monkeypatch.setattr(
        "service.drama_subtitle_translation.target_languages_for",
        lambda _source: ("en",),
    )
    monkeypatch.setattr(
        "service.drama_subtitle_translation.translate_with_configured_provider",
        lambda **_kwargs: {
            "text": translated_text,
            "cache_hit": True,
            "provider": "test",
            "duration_seconds": 0.0,
        },
    )

    def lexical_only_search(**kwargs: object) -> dict[str, object]:
        search_calls.append(bool(kwargs.get("semantic_enabled")))
        if kwargs.get("semantic_enabled"):
            raise AssertionError("high-confidence lexical dialogue should not require semantic follow-up")
        return {
            "candidate_count": 1,
            "candidates": [
                {
                    "rank": 1,
                    "book_id": "book-lexical-only",
                    "book_name": "Lexical Evidence Drama",
                    "episode_order": 1,
                    "language_code": "en",
                    "retrieval_sources": ["lexical"],
                    "match_metrics": {
                        "shared_trigram_count": 12,
                        "query_coverage_rate": 0.03,
                        "evidence_coverage_rate": 0.14,
                    },
                    "evidence": {
                        "window_uid": "book-lexical-only:1:1-16",
                        "window_text": translated_text,
                    },
                }
            ],
            "decision": {
                "matched": False,
                "status": "not_matched",
                "outcome": "no_match",
                "content_match_status": "not_matched",
                "title_resolution": "not_applicable",
            },
        }

    result = apply_translation_fallback(
        native_payload={"query_text": "原字幕", "query_language_code": "ja", "decision": {"outcome": "no_match"}},
        query_text="原字幕",
        search_function=lexical_only_search,
        search_kwargs={"semantic_enabled": True},
        config=DramaSubtitleTranslationConfig(enabled=True, deepl_api_key="test-key"),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert search_calls == [False]
    assert result["decision"]["status"] == "review_required"
    assert result["decision"]["reason"] == "translation_lexical_continuous_alignment_requires_review"
    assert result["decision"]["book_id"] == "book-lexical-only"


def test_tencent_provider_is_preferred_and_uses_private_cache(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    calls: list[object] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "Response": {
                        "TargetText": "The weather is beautiful today.",
                        "RequestId": "request-1",
                        "UsedAmount": 31,
                    }
                }
            ).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        calls.append(request)
        assert request.full_url == TENCENT_TMT_ENDPOINT
        assert request.get_header("Authorization").startswith("TC3-HMAC-SHA256 Credential=test-id/")
        assert request.data and b"Text" in request.data
        return FakeResponse()

    monkeypatch.setattr("service.drama_subtitle_translation.urlopen", fake_urlopen)
    config = DramaSubtitleTranslationConfig(
        enabled=True,
        deepl_api_key="deepL-fallback-key",
        tencent_secret_id="test-id",
        tencent_secret_key="test-secret",
        provider_order=("tencent", "deepl"),
    )
    first = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="今天天气很好。",
        search_function=_translated_payload,
        search_kwargs={},
        config=config,
        business_db_path=str(db_path),
        owner_user_id=1,
    )
    second = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="今天天气很好。",
        search_function=_translated_payload,
        search_kwargs={},
        config=config,
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    assert first["translation_fallback"]["attempts"][0]["provider"] == TENCENT_PROVIDER_KEY
    assert first["translation_fallback"]["attempts"][0]["cache_hit"] is False
    assert second["translation_fallback"]["attempts"][0]["provider"] == TENCENT_PROVIDER_KEY
    assert second["translation_fallback"]["attempts"][0]["cache_hit"] is True
    assert len(calls) == 1


def test_tencent_failure_falls_back_to_deepl_and_is_recorded(tmp_path, monkeypatch):
    db_path = tmp_path / "business.sqlite3"
    init_business_db(db_path)
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: dict):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url == TENCENT_TMT_ENDPOINT:
            return FakeResponse(
                {"Response": {"Error": {"Code": "LimitExceeded", "Message": "quota unavailable"}}}
            )
        return FakeResponse({"translations": [{"text": "translated subtitle"}]})

    monkeypatch.setattr("service.drama_subtitle_translation.urlopen", fake_urlopen)
    result = apply_translation_fallback(
        native_payload=_native_payload(),
        query_text="source subtitle",
        search_function=_translated_payload,
        search_kwargs={},
        config=DramaSubtitleTranslationConfig(
            enabled=True,
            deepl_api_key="deepL-fallback-key",
            tencent_secret_id="test-id",
            tencent_secret_key="test-secret",
            provider_order=("tencent", "deepl"),
        ),
        business_db_path=str(db_path),
        owner_user_id=1,
    )

    attempt = result["translation_fallback"]["attempts"][0]
    assert attempt["provider"] == "deepl-free-v2"
    assert attempt["provider_failures"] == [
        {"provider": "tencent", "error": "Tencent request failed: LimitExceeded: quota unavailable"}
    ]
    assert calls == [TENCENT_TMT_ENDPOINT, "https://api-free.deepl.com/v2/translate"]
