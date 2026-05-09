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


def _prepare_scoring_text(text: str, ngram_size: int) -> tuple[str, set[str]]:
    scoring_text = normalize_scoring_text(text)
    if not scoring_text:
        return "", set()
    return scoring_text, set(ordered_unique_ngrams(scoring_text, ngram_size))


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
    matcher = SequenceMatcher(a=query_scoring, b=candidate_scoring)
    sequence_ratio = matcher.ratio()
    length_ratio = min(len(query_scoring), len(candidate_scoring)) / max(
        len(query_scoring),
        len(candidate_scoring),
    )
    longest_match = matcher.find_longest_match(0, len(query_scoring), 0, len(candidate_scoring))
    longest_match_len = longest_match.size
    longest_match_ratio = longest_match_len / len(query_scoring)
    longest_match_precision = longest_match_len / len(candidate_scoring)
    matched_substring = query_scoring[longest_match.a : longest_match.a + longest_match_len]
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
) -> dict[str, float | str]:
    if not query_grams or not candidate_grams or not query_scoring or not candidate_scoring:
        return _empty_score_payload()

    intersection_size = len(query_grams & candidate_grams)
    union_size = len(query_grams | candidate_grams)
    ngram_recall = intersection_size / len(query_grams)
    ngram_precision = intersection_size / len(candidate_grams)
    jaccard = intersection_size / union_size if union_size else 0.0
    matcher = SequenceMatcher(a=query_scoring, b=candidate_scoring)
    sequence_ratio = matcher.ratio()
    length_ratio = min(len(query_scoring), len(candidate_scoring)) / max(
        len(query_scoring),
        len(candidate_scoring),
    )
    longest_match = matcher.find_longest_match(0, len(query_scoring), 0, len(candidate_scoring))
    longest_match_len = longest_match.size
    longest_match_ratio = longest_match_len / len(query_scoring)
    longest_match_precision = longest_match_len / len(candidate_scoring)
    matched_substring = query_scoring[longest_match.a : longest_match.a + longest_match_len]
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
    if not query_windows or not prepared_candidate_windows:
        return None

    best_match: dict[str, Any] | None = None
    for query_window in query_windows:
        for candidate_window in prepared_candidate_windows:
            metrics = _score_prepared_text_pair(
                query_scoring=str(query_window["scoring_text"]),
                query_grams=set(query_window["ngrams"]),
                candidate_scoring=str(candidate_window["scoring_text"]),
                candidate_grams=set(candidate_window["ngrams"]),
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
                "candidate_start_offset": int(candidate_window["start_offset"]),
                "candidate_end_offset": int(candidate_window["end_offset"]),
                "candidate_text": str(candidate_window["text"]),
                "candidate_text_preview": str(candidate_window["text_preview"]),
            }
            if best_match is None:
                best_match = current
                continue
            if (
                current["score"],
                bool(current["exact_substring_hit"]),
                current["longest_match_ratio"],
                current["ngram_recall"],
                current["sequence_ratio"],
                current["jaccard"],
                -abs(len(current["query_text"]) - len(current["candidate_text"])),
                -current["candidate_start_offset"],
            ) > (
                best_match["score"],
                bool(best_match["exact_substring_hit"]),
                best_match["longest_match_ratio"],
                best_match["ngram_recall"],
                best_match["sequence_ratio"],
                best_match["jaccard"],
                -abs(len(best_match["query_text"]) - len(best_match["candidate_text"])),
                -best_match["candidate_start_offset"],
            ):
                best_match = current

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
