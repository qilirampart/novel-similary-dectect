from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from retrieve_candidates_v1 import normalize_scoring_text, ordered_unique_ngrams  # noqa: E402

from service.candidate_slicing import slice_text_windows


def clip_text(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if limit <= 0 or len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _empty_score_payload() -> dict[str, float | str]:
    return {
        "score": 0.0,
        "ngram_recall": 0.0,
        "ngram_precision": 0.0,
        "jaccard": 0.0,
        "sequence_ratio": 0.0,
        "length_ratio": 0.0,
        "longest_match_len": 0.0,
        "longest_match_ratio": 0.0,
        "longest_match_precision": 0.0,
        "exact_substring_hit": 0.0,
        "matched_substring": "",
        "confidence_label": "weak",
        "review_label": "寮辫瘉鎹?",
    }


def _sequence_match_metrics(
    query_scoring: str,
    candidate_scoring: str,
    matcher: SequenceMatcher | None = None,
) -> tuple[float, int, str]:
    matcher = matcher or SequenceMatcher(a=query_scoring, b=candidate_scoring)
    if matcher is not None and (matcher.a != query_scoring or matcher.b != candidate_scoring):
        matcher.set_seqs(query_scoring, candidate_scoring)
    matching_blocks = matcher.get_matching_blocks()
    total_matched = 0
    longest_match_len = 0
    longest_match_start = 0
    for block in matching_blocks:
        size = int(block.size)
        total_matched += size
        if size > longest_match_len:
            longest_match_len = size
            longest_match_start = int(block.a)

    total_length = len(query_scoring) + len(candidate_scoring)
    sequence_ratio = (2.0 * total_matched / total_length) if total_length > 0 else 0.0
    matched_substring = (
        query_scoring[longest_match_start : longest_match_start + longest_match_len]
        if longest_match_len > 0
        else ""
    )
    return sequence_ratio, longest_match_len, matched_substring


def _prepare_scoring_text(text: str, ngram_size: int) -> tuple[str, set[str]]:
    scoring_text = normalize_scoring_text(text)
    if not scoring_text:
        return "", set()
    return scoring_text, set(ordered_unique_ngrams(scoring_text, ngram_size))


def _max_possible_sequence_ratio(query_len: int, candidate_len: int) -> float:
    total = query_len + candidate_len
    if total <= 0:
        return 0.0
    return 2.0 * min(query_len, candidate_len) / total


def _prepare_windows(
    windows: list[dict[str, Any]],
    *,
    ngram_size: int,
    preview_limit: int,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for window in windows:
        text = str(window["text"])
        scoring_text, grams = _prepare_scoring_text(text, ngram_size)
        prepared.append(
            {
                "window_order": int(window["window_order"]),
                "start_offset": int(window["start_offset"]),
                "end_offset": int(window["end_offset"]),
                "text": text,
                "text_preview": clip_text(text, limit=preview_limit),
                "scoring_text": scoring_text,
                "ngrams": grams,
            }
        )
    return prepared


def _match_priority_key(
    *,
    score: float,
    exact_substring_hit: bool,
    longest_match_ratio: float,
    ngram_recall: float,
    sequence_ratio: float,
    jaccard: float,
    query_scoring_len: int,
    candidate_scoring_len: int,
    candidate_start_offset: int,
) -> tuple[float, int, float, float, float, float, int, int]:
    return (
        float(score),
        1 if exact_substring_hit else 0,
        float(longest_match_ratio),
        float(ngram_recall),
        float(sequence_ratio),
        float(jaccard),
        -abs(int(query_scoring_len) - int(candidate_scoring_len)),
        -int(candidate_start_offset),
    )


def _match_upper_bound_key(
    *,
    query_scoring: str,
    query_grams: set[str],
    candidate_scoring: str,
    candidate_grams: set[str],
    ngram_size: int,
    sequence_ratio_upper: float,
    candidate_start_offset: int,
) -> tuple[float, int, float, float, float, float, int, int]:
    if not query_scoring or not candidate_scoring or not query_grams or not candidate_grams:
        return _match_priority_key(
            score=0.0,
            exact_substring_hit=False,
            longest_match_ratio=0.0,
            ngram_recall=0.0,
            sequence_ratio=0.0,
            jaccard=0.0,
            query_scoring_len=len(query_scoring),
            candidate_scoring_len=len(candidate_scoring),
            candidate_start_offset=candidate_start_offset,
        )

    query_len = len(query_scoring)
    candidate_len = len(candidate_scoring)
    shorter_len = min(query_len, candidate_len)
    longer_len = max(query_len, candidate_len)
    intersection_size = len(query_grams & candidate_grams)
    if intersection_size <= 0:
        return _match_priority_key(
            score=0.0,
            exact_substring_hit=False,
            longest_match_ratio=0.0,
            ngram_recall=0.0,
            sequence_ratio=0.0,
            jaccard=0.0,
            query_scoring_len=query_len,
            candidate_scoring_len=candidate_len,
            candidate_start_offset=candidate_start_offset,
        )

    ngram_recall = intersection_size / len(query_grams)
    ngram_precision = intersection_size / len(candidate_grams)
    union_size = len(query_grams | candidate_grams)
    jaccard = intersection_size / union_size if union_size else 0.0
    length_ratio = shorter_len / longer_len if longer_len > 0 else 0.0
    sequence_ratio = max(0.0, min(1.0, float(sequence_ratio_upper)))
    longest_match_len_upper = min(query_len, candidate_len, intersection_size + max(0, ngram_size - 1))
    longest_match_ratio = longest_match_len_upper / query_len if query_len > 0 else 0.0
    longest_match_precision = longest_match_len_upper / candidate_len if candidate_len > 0 else 0.0
    exact_substring_hit = query_scoring in candidate_scoring

    score = (
        0.30 * longest_match_ratio
        + 0.25 * ngram_recall
        + 0.10 * ngram_precision
        + 0.10 * jaccard
        + 0.10 * sequence_ratio
        + 0.05 * length_ratio
        + 0.10 * longest_match_precision
    )
    if exact_substring_hit:
        score += 0.15

    return _match_priority_key(
        score=min(score, 1.0),
        exact_substring_hit=exact_substring_hit,
        longest_match_ratio=longest_match_ratio,
        ngram_recall=ngram_recall,
        sequence_ratio=sequence_ratio,
        jaccard=jaccard,
        query_scoring_len=query_len,
        candidate_scoring_len=candidate_len,
        candidate_start_offset=candidate_start_offset,
    )


def _candidate_can_beat_current_best(
    *,
    query_scoring: str,
    query_grams: set[str],
    candidate_scoring: str,
    candidate_grams: set[str],
    current_best_key: tuple[float, int, float, float, float, float, int, int] | None,
    candidate_start_offset: int,
) -> bool:
    if current_best_key is None:
        return True
    if not query_scoring or not candidate_scoring or not query_grams or not candidate_grams:
        return False

    query_len = len(query_scoring)
    candidate_len = len(candidate_scoring)
    if query_len <= 0 or candidate_len <= 0:
        return False

    intersection_size = len(query_grams & candidate_grams)
    if intersection_size <= 0:
        return False

    ngram_recall = intersection_size / len(query_grams)
    ngram_precision = intersection_size / len(candidate_grams)
    union_size = len(query_grams | candidate_grams)
    jaccard = intersection_size / union_size if union_size else 0.0
    length_ratio = min(query_len, candidate_len) / max(query_len, candidate_len)
    longest_match_ratio_upper = min(1.0, intersection_size / query_len)
    longest_match_precision_upper = min(1.0, intersection_size / candidate_len)
    sequence_ratio_upper = _max_possible_sequence_ratio(query_len, candidate_len)

    score_upper = (
        0.30 * longest_match_ratio_upper
        + 0.25 * ngram_recall
        + 0.10 * ngram_precision
        + 0.10 * jaccard
        + 0.10 * sequence_ratio_upper
        + 0.05 * length_ratio
        + 0.10 * longest_match_precision_upper
    )
    if query_scoring in candidate_scoring:
        score_upper += 0.15
    score_upper = min(score_upper, 1.0)

    upper_key = _match_priority_key(
        score=score_upper,
        exact_substring_hit=query_scoring in candidate_scoring,
        longest_match_ratio=longest_match_ratio_upper,
        ngram_recall=ngram_recall,
        sequence_ratio=sequence_ratio_upper,
        jaccard=jaccard,
        query_scoring_len=query_len,
        candidate_scoring_len=candidate_len,
        candidate_start_offset=candidate_start_offset,
    )
    return upper_key > current_best_key


def classify_confidence(
    exact_substring_hit: bool,
    longest_match_ratio: float,
    ngram_recall: float,
    ngram_precision: float,
    jaccard: float,
    sequence_ratio: float,
    length_ratio: float,
) -> tuple[str, str]:
    structural_strong_hit = (
        sequence_ratio >= 0.93
        and jaccard >= 0.75
        and ngram_recall >= 0.84
        and ngram_precision >= 0.84
        and length_ratio >= 0.92
    )
    structural_medium_hit = (
        sequence_ratio >= 0.86
        and jaccard >= 0.62
        and ngram_recall >= 0.72
        and ngram_precision >= 0.72
        and length_ratio >= 0.85
    )
    if exact_substring_hit and longest_match_ratio >= 0.95:
        return "exact_quote", "强证据"
    if longest_match_ratio >= 0.70 and ngram_recall >= 0.80:
        return "strong", "强证据"
    if structural_strong_hit:
        return "strong", "强证据"
    if longest_match_ratio >= 0.45 and ngram_recall >= 0.55:
        return "medium", "中证据"
    if structural_medium_hit:
        return "medium", "中证据"
    return "weak", "弱证据"


def score_text_pair(query_text: str, candidate_text: str, ngram_size: int = 3) -> dict[str, float]:
    if ngram_size <= 0:
        raise ValueError("ngram_size must be > 0")

    query_scoring, query_grams = _prepare_scoring_text(query_text, ngram_size)
    candidate_scoring, candidate_grams = _prepare_scoring_text(candidate_text, ngram_size)
    if not query_grams or not candidate_grams:
        return _empty_score_payload()

    intersection_size = len(query_grams & candidate_grams)
    union_size = len(query_grams | candidate_grams)
    ngram_recall = intersection_size / len(query_grams)
    ngram_precision = intersection_size / len(candidate_grams)
    jaccard = intersection_size / union_size if union_size else 0.0
    sequence_ratio, longest_match_len, matched_substring = _sequence_match_metrics(
        query_scoring=query_scoring,
        candidate_scoring=candidate_scoring,
    )
    length_ratio = min(len(query_scoring), len(candidate_scoring)) / max(
        len(query_scoring),
        len(candidate_scoring),
    )
    longest_match_ratio = longest_match_len / len(query_scoring)
    longest_match_precision = longest_match_len / len(candidate_scoring)
    exact_substring_hit = query_scoring in candidate_scoring
    structural_rewrite_hit = (
        sequence_ratio >= 0.90
        and jaccard >= 0.70
        and ngram_recall >= 0.80
        and ngram_precision >= 0.80
        and length_ratio >= 0.90
    )

    score = (
        0.30 * longest_match_ratio
        + 0.25 * ngram_recall
        + 0.10 * ngram_precision
        + 0.10 * jaccard
        + 0.10 * sequence_ratio
        + 0.05 * length_ratio
        + 0.10 * longest_match_precision
    )
    if exact_substring_hit:
        score += 0.15
    elif longest_match_ratio < 0.25 and not structural_rewrite_hit:
        score *= 0.55
    elif longest_match_ratio < 0.40 and not structural_rewrite_hit:
        score *= 0.75

    confidence_label, review_label = classify_confidence(
        exact_substring_hit=exact_substring_hit,
        longest_match_ratio=longest_match_ratio,
        ngram_recall=ngram_recall,
        ngram_precision=ngram_precision,
        jaccard=jaccard,
        sequence_ratio=sequence_ratio,
        length_ratio=length_ratio,
    )
    return {
        "score": min(score, 1.0),
        "ngram_recall": ngram_recall,
        "ngram_precision": ngram_precision,
        "jaccard": jaccard,
        "sequence_ratio": sequence_ratio,
        "length_ratio": length_ratio,
        "longest_match_len": float(longest_match_len),
        "longest_match_ratio": longest_match_ratio,
        "longest_match_precision": longest_match_precision,
        "exact_substring_hit": float(1.0 if exact_substring_hit else 0.0),
        "matched_substring": matched_substring,
        "confidence_label": confidence_label,
        "review_label": review_label,
    }


def _score_prepared_text_pair(
    *,
    query_scoring: str,
    query_grams: set[str],
    candidate_scoring: str,
    candidate_grams: set[str],
    matcher: SequenceMatcher | None = None,
) -> dict[str, float | str]:
    if not query_grams or not candidate_grams or not query_scoring or not candidate_scoring:
        return _empty_score_payload()

    intersection_size = len(query_grams & candidate_grams)
    if intersection_size <= 0:
        return _empty_score_payload()
    union_size = len(query_grams | candidate_grams)
    ngram_recall = intersection_size / len(query_grams)
    ngram_precision = intersection_size / len(candidate_grams)
    jaccard = intersection_size / union_size if union_size else 0.0
    sequence_ratio, longest_match_len, matched_substring = _sequence_match_metrics(
        query_scoring=query_scoring,
        candidate_scoring=candidate_scoring,
        matcher=matcher,
    )
    length_ratio = min(len(query_scoring), len(candidate_scoring)) / max(
        len(query_scoring),
        len(candidate_scoring),
    )
    longest_match_ratio = longest_match_len / len(query_scoring)
    longest_match_precision = longest_match_len / len(candidate_scoring)
    exact_substring_hit = query_scoring in candidate_scoring
    structural_rewrite_hit = (
        sequence_ratio >= 0.90
        and jaccard >= 0.70
        and ngram_recall >= 0.80
        and ngram_precision >= 0.80
        and length_ratio >= 0.90
    )

    score = (
        0.30 * longest_match_ratio
        + 0.25 * ngram_recall
        + 0.10 * ngram_precision
        + 0.10 * jaccard
        + 0.10 * sequence_ratio
        + 0.05 * length_ratio
        + 0.10 * longest_match_precision
    )
    if exact_substring_hit:
        score += 0.15
    elif longest_match_ratio < 0.25 and not structural_rewrite_hit:
        score *= 0.55
    elif longest_match_ratio < 0.40 and not structural_rewrite_hit:
        score *= 0.75

    confidence_label, review_label = classify_confidence(
        exact_substring_hit=exact_substring_hit,
        longest_match_ratio=longest_match_ratio,
        ngram_recall=ngram_recall,
        ngram_precision=ngram_precision,
        jaccard=jaccard,
        sequence_ratio=sequence_ratio,
        length_ratio=length_ratio,
    )
    return {
        "score": min(score, 1.0),
        "ngram_recall": ngram_recall,
        "ngram_precision": ngram_precision,
        "jaccard": jaccard,
        "sequence_ratio": sequence_ratio,
        "length_ratio": length_ratio,
        "longest_match_len": float(longest_match_len),
        "longest_match_ratio": longest_match_ratio,
        "longest_match_precision": longest_match_precision,
        "exact_substring_hit": float(1.0 if exact_substring_hit else 0.0),
        "matched_substring": matched_substring,
        "confidence_label": confidence_label,
        "review_label": review_label,
    }


def find_best_window_match(
    query_text: str,
    candidate_windows: list[dict[str, Any]],
    window_size: int = 200,
    step_size: int = 50,
    ngram_size: int = 3,
    prepared_query_windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not query_text.strip():
        raise ValueError("query_text is empty")

    if prepared_query_windows is None:
        query_windows = _prepare_windows(
            slice_text_windows(query_text, window_size=window_size, step_size=step_size),
            ngram_size=ngram_size,
            preview_limit=80,
        )
    else:
        query_windows = prepared_query_windows
    prepared_candidate_windows = _prepare_windows(
        candidate_windows,
        ngram_size=ngram_size,
        preview_limit=160,
    )
    candidate_matchers = [
        SequenceMatcher(a="", b=str(candidate_window["scoring_text"]))
        for candidate_window in prepared_candidate_windows
    ]
    if not query_windows or not prepared_candidate_windows:
        return None

    best_match: dict[str, Any] | None = None
    best_match_key: tuple[float, int, float, float, float, float, int, int] | None = None
    for query_window in query_windows:
        query_scoring = str(query_window["scoring_text"])
        query_grams = query_window["ngrams"]
        for candidate_window, matcher in zip(prepared_candidate_windows, candidate_matchers):
            candidate_scoring = str(candidate_window["scoring_text"])
            candidate_grams = candidate_window["ngrams"]
            candidate_start_offset = int(candidate_window["start_offset"])

            if not _candidate_can_beat_current_best(
                query_scoring=query_scoring,
                query_grams=query_grams,
                candidate_scoring=candidate_scoring,
                candidate_grams=candidate_grams,
                current_best_key=best_match_key,
                candidate_start_offset=candidate_start_offset,
            ):
                continue

            matcher.set_seq1(query_scoring)
            metrics = _score_prepared_text_pair(
                query_scoring=query_scoring,
                query_grams=query_grams,
                candidate_scoring=candidate_scoring,
                candidate_grams=candidate_grams,
                matcher=matcher,
            )
            current = {
                "score": float(metrics["score"]),
                "ngram_recall": float(metrics["ngram_recall"]),
                "ngram_precision": float(metrics["ngram_precision"]),
                "jaccard": float(metrics["jaccard"]),
                "sequence_ratio": float(metrics["sequence_ratio"]),
                "length_ratio": float(metrics["length_ratio"]),
                "longest_match_len": int(metrics["longest_match_len"]),
                "longest_match_ratio": float(metrics["longest_match_ratio"]),
                "longest_match_precision": float(metrics["longest_match_precision"]),
                "exact_substring_hit": bool(metrics["exact_substring_hit"]),
                "matched_substring": str(metrics["matched_substring"]),
                "confidence_label": str(metrics["confidence_label"]),
                "review_label": str(metrics["review_label"]),
                "query_window_order": int(query_window["window_order"]),
                "query_start_offset": int(query_window["start_offset"]),
                "query_end_offset": int(query_window["end_offset"]),
                "query_text": str(query_window["text"]),
                "query_text_preview": str(query_window["text_preview"]),
                "candidate_window_order": int(candidate_window["window_order"]),
                "candidate_start_offset": candidate_start_offset,
                "candidate_end_offset": int(candidate_window["end_offset"]),
                "candidate_text": str(candidate_window["text"]),
                "candidate_text_preview": str(candidate_window["text_preview"]),
            }
            current_key = _match_priority_key(
                score=float(current["score"]),
                exact_substring_hit=bool(current["exact_substring_hit"]),
                longest_match_ratio=float(current["longest_match_ratio"]),
                ngram_recall=float(current["ngram_recall"]),
                sequence_ratio=float(current["sequence_ratio"]),
                jaccard=float(current["jaccard"]),
                query_scoring_len=len(query_scoring),
                candidate_scoring_len=len(candidate_scoring),
                candidate_start_offset=candidate_start_offset,
            )
            if best_match is None:
                best_match = current
                best_match_key = current_key
                continue
            if best_match_key is None or current_key > best_match_key:
                best_match = current
                best_match_key = current_key

    if best_match is None:
        return None

    best_match["query_window_count"] = len(query_windows)
    best_match["candidate_window_count"] = len(candidate_windows)
    return best_match


def compare_query_to_candidates(
    query_text: str,
    candidates: list[dict[str, Any]],
    sliced_candidates: dict[int, dict[str, Any]],
    window_size: int = 200,
    step_size: int = 50,
    ngram_size: int = 3,
) -> list[dict[str, Any]]:
    if not query_text.strip():
        raise ValueError("query_text is empty")

    prepared_query_windows = _prepare_windows(
        slice_text_windows(query_text, window_size=window_size, step_size=step_size),
        ngram_size=ngram_size,
        preview_limit=80,
    )
    if not prepared_query_windows:
        return []

    compared: list[dict[str, Any]] = []
    for coarse_rank, candidate in enumerate(candidates, start=1):
        chapter_uid = int(candidate["chapter_uid"])
        sliced = sliced_candidates.get(chapter_uid)
        if not sliced or not sliced["windows"]:
            continue

        best_match = find_best_window_match(
            query_text=query_text,
            candidate_windows=list(sliced["windows"]),
            window_size=window_size,
            step_size=step_size,
            ngram_size=ngram_size,
            prepared_query_windows=prepared_query_windows,
        )
        if not best_match:
            continue

        compared.append(
            {
                "chapter_uid": chapter_uid,
                "dataset_key": candidate["dataset_key"],
                "book_ext_id": candidate["book_ext_id"],
                "book_name": candidate["book_name"],
                "chapter_ext_id": candidate["chapter_ext_id"],
                "chapter_name": candidate["chapter_name"],
                "source_dataset_key": candidate.get("source_dataset_key", candidate["dataset_key"]),
                "source_table_name": candidate.get("source_table_name", ""),
                "coarse_rank": coarse_rank,
                "coarse_final_score": float(candidate["final_score"]),
                "coarse_ngram_score": float(candidate["ngram_score"]),
                "coarse_seed_hit_count": int(candidate["seed_hit_count"]),
                "coarse_seed_hit_weight": float(candidate["seed_hit_weight"]),
                "candidate_char_count": int(sliced["char_count"]),
                "candidate_window_count": int(sliced["window_count"]),
                "fine_score": float(best_match["score"]),
                "confidence_label": str(best_match["confidence_label"]),
                "review_label": str(best_match["review_label"]),
                "best_match": best_match,
            }
        )

    compared.sort(
        key=lambda item: (
            -item["fine_score"],
            -int(bool(item["best_match"]["exact_substring_hit"])),
            -float(item["best_match"]["longest_match_ratio"]),
            -float(item["best_match"]["ngram_recall"]),
            -float(item["best_match"]["sequence_ratio"]),
            -item["coarse_final_score"],
            int(item["chapter_uid"]),
        )
    )

    for fine_rank, item in enumerate(compared, start=1):
        item["fine_rank"] = fine_rank
    return compared
