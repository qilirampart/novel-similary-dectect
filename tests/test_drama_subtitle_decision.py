from service.drama_subtitle_decision import decide_drama_subtitle_match


def candidate(book_id="book-1", episode=1, *, lexical=False, shared=0, query_count=100, query_rate=0.0, evidence_rate=0.0, semantic=None, rank=1, language="en", evidence_text=""):
    result = {
        "book_id": book_id,
        "book_name": book_id,
        "episode_order": episode,
        "language_code": language,
        "rank": rank,
        "semantic_score": semantic,
        "evidence": {"window_uid": f"{book_id}:{episode}", "window_text": evidence_text},
    }
    if lexical:
        result["retrieval_sources"] = ["lexical"]
        result["match_metrics"] = {
            "shared_trigram_count": shared,
            "query_trigram_count": query_count,
            "query_coverage_rate": query_rate,
            "evidence_coverage_rate": evidence_rate,
            "match_unit": "character_trigram" if language == "zh" else "word_4gram",
            "match_unit_label": "共享三元词" if language == "zh" else "共享四词短语",
        }
    else:
        result["retrieval_sources"] = ["semantic"]
    return result


def test_strong_lexical_match_and_related_episode_are_separate():
    result = decide_drama_subtitle_match(
        candidates=[
            candidate(lexical=True, shared=20, query_count=100, query_rate=0.2, evidence_rate=0.8),
            candidate(lexical=True, episode=2, shared=4, query_count=100, query_rate=0.04, evidence_rate=0.2, rank=2),
            candidate(book_id="noise", semantic=0.91, rank=3),
        ],
        semantic_status="ok",
        query_language_code="en",
    )
    assert result["status"] == "matched"
    assert result["hit_status"] == "matched"
    assert result["is_confirmed_match"] is True
    assert result["book_id"] == "book-1"
    assert result["matched_episode_order"] == 1
    assert result["related_episode_orders"] == [2]


def test_semantic_only_candidate_requires_review_not_match():
    result = decide_drama_subtitle_match(
        candidates=[candidate(semantic=0.90)], semantic_status="ok", query_language_code="en"
    )
    assert result["status"] == "review_required"
    assert result["matched"] is False
    assert result["hit_status"] == "review_required"
    assert result["is_confirmed_match"] is False
    assert result["review_feedback"]["reason_type"] == "semantic_similarity_without_sufficient_lexical_evidence"


def test_semantic_baseline_is_not_a_match():
    result = decide_drama_subtitle_match(
        candidates=[candidate(semantic=0.68)], semantic_status="ok", query_language_code="en"
    )
    assert result["status"] == "not_matched"
    assert result["hit_status"] == "not_matched"
    assert result["is_confirmed_match"] is False


def test_multiple_strong_books_require_review():
    result = decide_drama_subtitle_match(
        candidates=[
            candidate(book_id="book-1", lexical=True, shared=20, query_rate=0.2, evidence_rate=0.8),
            candidate(book_id="book-2", lexical=True, shared=20, query_rate=0.2, evidence_rate=0.8, rank=2),
        ],
        semantic_status="ok",
        query_language_code="en",
    )
    assert result["status"] == "review_required"
    assert result["hit_status"] == "review_required"
    assert result["is_confirmed_match"] is False
    assert result["reason"] == "multiple_books_with_strong_lexical_evidence"
    assert result["outcome"] == "content_matched_ambiguous"
    assert result["content_match_status"] == "matched"
    assert result["title_resolution"] == "ambiguous"
    assert "《book-1》与《book-2》" in result["user_message"]
    assert result["strong_match_candidates"][0]["review_priority"] == 1
    assert result["strong_match_candidates"][0]["text_coverage_rate"] == 0.2
    assert result["strong_match_candidates"][0]["evidence_coverage_rate"] == 0.8
    assert result["review_feedback"]["reason_type"] == "multiple_strong_content_versions"
    assert len(result["strong_match_candidates"]) == 2


def test_weak_lexical_review_explains_why_confirmation_is_unsafe():
    result = decide_drama_subtitle_match(
        candidates=[candidate(lexical=True, shared=8, query_rate=0.07, evidence_rate=0.2, semantic=0.76)],
        semantic_status="ok",
        query_language_code="en",
    )

    assert result["outcome"] == "potential_match"
    assert result["review_feedback"]["reason_type"] == "partial_continuous_dialogue_overlap"
    assert "共享四词短语" in result["review_feedback"]["summary"]
    assert "需复核" in result["user_message"]


def test_semantic_fallback_explains_missing_semantic_evidence():
    result = decide_drama_subtitle_match(
        candidates=[candidate(semantic=0.9)],
        semantic_status="fallback_lexical_only",
        query_language_code="en",
    )
    assert result["status"] == "not_matched"
    assert result["reason"] == "semantic_unavailable_no_lexical_evidence"


def test_chinese_asr_variant_with_strong_ordered_alignment_is_confirmed():
    query_text = "订婚前夕我的未婚夫傅昱航和他的女兄弟在夜店扯了结婚证恭喜傅少新婚快乐我们说到做到你们俩玩的这么疯"
    evidence_text = "订婚前夕我的未婚夫傅屿杭和他的女兄弟在夜店扯了结婚证恭喜傅少新婚快乐我们说到做到你们俩玩的这么疯"
    result = decide_drama_subtitle_match(
        candidates=[candidate(
            lexical=True,
            shared=40,
            query_rate=0.12,
            evidence_rate=0.50,
            language="zh",
            evidence_text=evidence_text,
        )],
        semantic_status="ok",
        query_language_code="zh",
        query_text=query_text,
    )

    assert result["status"] == "matched"
    assert result["reason"] == "strong_fuzzy_lexical_evidence"
    assert result["fuzzy_alignment_score"] >= 0.78


def test_chinese_low_ordered_alignment_stays_in_review():
    result = decide_drama_subtitle_match(
        candidates=[candidate(
            lexical=True,
            shared=20,
            query_rate=0.10,
            evidence_rate=0.40,
            language="zh",
            evidence_text="新年快乐大家多吃点菜最后一道菜了",
        )],
        semantic_status="ok",
        query_language_code="zh",
        query_text="婚礼现场未婚夫背叛我我决定离婚并离开这座城市",
    )

    assert result["status"] == "review_required"
