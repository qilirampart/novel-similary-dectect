from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scripts.v2_common import connect_db
from service.drama_subtitle_language import SUPPORTED_LANGUAGE_CODES, classify_subtitle_text


DEFAULT_CANDIDATE_LIMIT = 10
DEFAULT_WINDOW_LIMIT = 200
LANGUAGE_FTS_SUFFIX = "_lang_v2"
WHITESPACE_RE = re.compile(r"\s+")
BRACKETED_STAGE_DIRECTION_RE = re.compile(r"\[[^\]]{1,80}\]")
ENGLISH_WORD_RE = re.compile(r"[A-Za-z0-9]+")
ENGLISH_PHRASE_SIZE = 4
WORD_PHRASE_LANGUAGE_CODES = {"en", "pt"}


class DramaSubtitleRetrievalError(RuntimeError):
    pass


def _unique_trigrams(text: str) -> set[str]:
    compact = normalize_query(text).replace(" ", "")
    return {compact[index : index + 3] for index in range(max(len(compact) - 2, 0))}


def _english_words(text: str) -> list[str]:
    return ENGLISH_WORD_RE.findall(normalize_query(text).lower())


def _unique_english_phrases(text: str, *, phrase_size: int = ENGLISH_PHRASE_SIZE) -> set[str]:
    words = _english_words(text)
    return {
        " ".join(words[index : index + phrase_size])
        for index in range(max(len(words) - phrase_size + 1, 0))
    }


def _match_units(text: str, *, match_unit: str) -> set[str]:
    return _unique_english_phrases(text) if match_unit == "word_4gram" else _unique_trigrams(text)


def _build_overlap_metrics(
    query_text: str,
    evidence_text: str,
    *,
    match_unit: str,
) -> dict[str, Any]:
    query_grams = _match_units(query_text, match_unit=match_unit)
    evidence_grams = _match_units(evidence_text, match_unit=match_unit)
    unit_label = "共享四词短语" if match_unit == "word_4gram" else "共享三元词"
    shared = sorted(query_grams & evidence_grams)
    query_count = len(query_grams)
    evidence_count = len(evidence_grams)
    return {
        "match_unit": match_unit,
        "match_unit_label": unit_label,
        "query_trigram_count": query_count,
        "shared_trigram_count": len(shared),
        "evidence_trigram_count": evidence_count,
        "query_coverage_rate": round(len(shared) / query_count, 4) if query_count else 0.0,
        "evidence_coverage_rate": round(len(shared) / evidence_count, 4) if evidence_count else 0.0,
        "shared_trigrams": shared[:80],
    }


def normalize_query(text: str) -> str:
    normalized = str(text or "").replace("\u3000", " ").replace("\xa0", " ").strip()
    # Caption tracks frequently include [music], [applause], and similar markers.
    # They are not dialogue and must not make API entry points score the same video differently.
    normalized = BRACKETED_STAGE_DIRECTION_RE.sub(" ", normalized)
    return WHITESPACE_RE.sub(" ", normalized).strip()


def build_trigram_match_query(query_text: str, *, max_grams: int = 80) -> tuple[str | None, str]:
    if max_grams <= 0:
        raise ValueError("max_grams must be > 0")
    normalized = normalize_query(query_text)
    compact = normalized.replace(" ", "")
    if len(compact) < 3:
        return None, normalized

    all_grams: list[str] = []
    seen: set[str] = set()
    for index in range(len(compact) - 2):
        gram = compact[index : index + 3]
        if gram in seen:
            continue
        seen.add(gram)
        all_grams.append(gram)
    if len(all_grams) > max_grams:
        # Preserve dense opening evidence while still sampling the full subtitle.
        # The previous implementation used only the opening and missed later scenes.
        head_count = max(1, max_grams // 2)
        tail_count = max_grams - head_count
        positions = set(range(head_count))
        if tail_count > 0:
            positions.update(
                round(head_count + index * (len(all_grams) - head_count - 1) / (tail_count - 1))
                for index in range(tail_count)
            ) if tail_count > 1 else positions.add(len(all_grams) - 1)
        grams = [gram for index, gram in enumerate(all_grams) if index in positions]
    else:
        grams = all_grams
    return " OR ".join('"' + gram.replace('"', '""') + '"' for gram in grams) or None, normalized


def build_english_phrase_match_query(query_text: str, *, max_phrases: int = 80) -> tuple[str | None, str]:
    if max_phrases <= 0:
        raise ValueError("max_phrases must be > 0")
    normalized = normalize_query(query_text)
    words = _english_words(normalized)
    if len(words) < ENGLISH_PHRASE_SIZE:
        return None, normalized

    phrases: list[str] = []
    seen: set[str] = set()
    for index in range(len(words) - ENGLISH_PHRASE_SIZE + 1):
        phrase = " ".join(words[index : index + ENGLISH_PHRASE_SIZE])
        if phrase not in seen:
            phrases.append(phrase)
            seen.add(phrase)
    if len(phrases) > max_phrases:
        positions = {
            round(index * (len(phrases) - 1) / (max_phrases - 1))
            for index in range(max_phrases)
        } if max_phrases > 1 else {0}
        phrases = [phrase for index, phrase in enumerate(phrases) if index in positions]
    return " OR ".join(f'"{phrase}"' for phrase in phrases) or None, normalized


def _language_fts_ready(conn: Any, table_name: str) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1
              FROM drama_subtitle_lexical_index_metadata
             WHERE index_name = ?
               AND build_status = 'ready'
               AND indexed_window_count = source_window_count
               AND indexed_window_count > 0
             LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        # Existing production databases do not have V2 metadata until the
        # offline index build completes, so they must remain on legacy FTS.
        return False


def _language_aware_match_query(match_query: str, language_code: str) -> str:
    """Restrict FTS itself to the requested corpus language before scoring."""
    language = language_code.strip().lower()
    if not language:
        return match_query
    return f'language_code : "lang_{language}" AND ({match_query})'


def _serialize_evidence(row: tuple[Any, ...], *, include_window_text: bool) -> dict[str, Any]:
    evidence = {
        "window_uid": row[0],
        "line_start": row[5],
        "line_end": row[6],
        "time_start": row[7],
        "time_end": row[8],
        "window_text_preview": row[9],
        "line_count": row[10],
        "char_count": row[11],
        "language_code": row[14],
        "language_confidence": row[15],
    }
    if include_window_text:
        evidence["window_text"] = row[12]
    return evidence


def _aggregate_rows(
    rows: list[tuple[Any, ...]],
    *,
    candidate_limit: int,
    include_window_text: bool,
    mode: str,
    query_text: str,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (str(row[1]), int(row[4]))
        score = float(row[13]) if mode in {"fts5_trigram", "fts5_word_phrase"} else None
        candidate = grouped.get(key)
        if candidate is None:
            candidate = {
                "book_id": row[1],
                "book_name": row[2],
                "episode_uid": row[3],
                "episode_order": row[4],
                "language_code": row[14],
                "language_confidence": row[15],
                "retrieved_window_count": 0,
                "best_lexical_score": score,
                "evidence": _serialize_evidence(row, include_window_text=include_window_text),
                # Keep retrieved texts only while deriving episode-level coverage below.
                "_retrieved_window_texts": [],
            }
            grouped[key] = candidate
        candidate["retrieved_window_count"] += 1
        candidate["_retrieved_window_texts"].append(str(row[12] or ""))
        if mode in {"fts5_trigram", "fts5_word_phrase"} and score is not None and score < candidate["best_lexical_score"]:
            candidate["best_lexical_score"] = score
            candidate["evidence"] = _serialize_evidence(row, include_window_text=include_window_text)

    candidates = list(grouped.values())
    if mode in {"fts5_trigram", "fts5_word_phrase"}:
        candidates.sort(
            key=lambda item: (
                item["best_lexical_score"],
                -item["retrieved_window_count"],
                item["book_id"],
                item["episode_order"],
            )
        )
    else:
        candidates.sort(
            key=lambda item: (
                -item["retrieved_window_count"],
                item["book_id"],
                item["episode_order"],
            )
        )

    # Only returned candidates need display and decision metrics. This avoids
    # re-tokenizing every raw FTS hit merely to calculate a UI-only aggregate.
    candidates = candidates[:candidate_limit]
    for candidate in candidates:
        evidence = candidate["evidence"]
        evidence_text = str(evidence.get("window_text") or evidence.get("window_text_preview") or "")
        match_unit = "word_4gram" if mode == "fts5_word_phrase" else "character_trigram"
        metrics = _build_overlap_metrics(
            query_text,
            evidence_text,
            match_unit=match_unit,
        )
        # A candidate episode can have several matching windows. The legacy coverage
        # stays tied to the best window for conservative decisions; this aggregate is
        # display-only and measures the union across every retrieved window.
        query_units = _match_units(query_text, match_unit=match_unit)
        retrieved_units: set[str] = set()
        for window_text in candidate.pop("_retrieved_window_texts", []):
            retrieved_units.update(_match_units(window_text, match_unit=match_unit))
        aggregate_shared = query_units & retrieved_units
        metrics["retrieved_window_query_coverage_rate"] = round(
            len(aggregate_shared) / len(query_units), 4
        ) if query_units else 0.0
        metrics["retrieved_window_evidence_coverage_rate"] = round(
            len(aggregate_shared) / len(retrieved_units), 4
        ) if retrieved_units else 0.0
        metrics["retrieved_window_shared_match_count"] = len(aggregate_shared)
        candidate["match_metrics"] = metrics

    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates


def search_drama_subtitle_lexical_candidates(
    *,
    db_path: str,
    query_text: str,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    window_limit: int = DEFAULT_WINDOW_LIMIT,
    book_id: str = "",
    include_window_text: bool = True,
    max_grams: int = 80,
    language_code: str = "",
) -> dict[str, Any]:
    if candidate_limit <= 0 or window_limit <= 0:
        raise ValueError("candidate_limit and window_limit must be > 0")
    resolved_db_path = Path(db_path)
    if not resolved_db_path.exists():
        raise DramaSubtitleRetrievalError(f"subtitle database not found: {resolved_db_path}")

    normalized_query = normalize_query(query_text)
    if not normalized_query:
        raise ValueError("query_text is empty")
    query_language = classify_subtitle_text(normalized_query)
    use_word_phrases = query_language.language_code in WORD_PHRASE_LANGUAGE_CODES
    if use_word_phrases:
        match_query, normalized_query = build_english_phrase_match_query(query_text, max_phrases=max_grams)
    else:
        match_query, normalized_query = build_trigram_match_query(query_text, max_grams=max_grams)
    normalized_book_id = book_id.strip()
    requested_language = language_code.strip().lower()
    if requested_language and requested_language not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(f"unsupported language_code: {requested_language}")
    effective_language = requested_language or (
        query_language.language_code if query_language.language_code in SUPPORTED_LANGUAGE_CODES else ""
    )

    conn = connect_db(resolved_db_path)
    conn.row_factory = None
    try:
        if match_query is None:
            mode = "like_fallback"
            rows = conn.execute(
                """
                SELECT w.window_uid,
                       w.book_id,
                       w.book_name,
                       w.episode_uid,
                       w.episode_order,
                       w.line_start,
                       w.line_end,
                       w.time_start,
                       w.time_end,
                       w.window_text_preview,
                       w.line_count,
                       w.char_count,
                       w.window_text,
                       NULL AS lexical_score,
                       w.language_code,
                       w.language_confidence
                  FROM drama_subtitle_windows w
                 WHERE w.window_text LIKE ?
                   AND (? = '' OR w.book_id = ?)
                   AND (? = '' OR w.language_code = ?)
                 ORDER BY w.book_id, w.episode_order, w.line_start
                 LIMIT ?
                """,
                (
                    f"%{normalized_query}%",
                    normalized_book_id,
                    normalized_book_id,
                    effective_language,
                    effective_language,
                    window_limit,
                ),
            ).fetchall()
        else:
            mode = "fts5_word_phrase" if use_word_phrases else "fts5_trigram"
            base_fts_table = "drama_subtitle_windows_word_fts" if use_word_phrases else "drama_subtitle_windows_fts"
            language_fts_table = f"{base_fts_table}{LANGUAGE_FTS_SUFFIX}"
            use_language_fts = bool(effective_language) and _language_fts_ready(conn, language_fts_table)
            fts_table = language_fts_table if use_language_fts else base_fts_table
            fts_match_query = (
                _language_aware_match_query(match_query, effective_language)
                if use_language_fts
                else match_query
            )
            lexical_score_expression = (
                f"bm25({fts_table}, 0.0, 0.0, 1.0)"
                if use_language_fts
                else f"bm25({fts_table})"
            )
            rows = conn.execute(
                f"""
                SELECT w.window_uid,
                       w.book_id,
                       w.book_name,
                       w.episode_uid,
                       w.episode_order,
                       w.line_start,
                       w.line_end,
                       w.time_start,
                       w.time_end,
                       w.window_text_preview,
                       w.line_count,
                       w.char_count,
                       w.window_text,
                       {lexical_score_expression} AS lexical_score,
                       w.language_code,
                       w.language_confidence
                  FROM {fts_table} f
                  JOIN drama_subtitle_windows w ON w.window_uid = f.window_uid
                 WHERE {fts_table} MATCH ?
                   AND (? = '' OR w.book_id = ?)
                   AND (? = '' OR w.language_code = ?)
                 ORDER BY lexical_score ASC, w.book_id, w.episode_order, w.line_start
                 LIMIT ?
                """,
                (
                    fts_match_query,
                    normalized_book_id,
                    normalized_book_id,
                    effective_language,
                    effective_language,
                    window_limit,
                ),
            ).fetchall()
    finally:
        conn.close()

    candidates = _aggregate_rows(
        rows,
        candidate_limit=candidate_limit,
        include_window_text=include_window_text,
        mode=mode,
        query_text=normalized_query,
    )
    return {
        "mode": mode,
        "query_text": normalized_query,
        "query_language_code": query_language.language_code,
        "query_language_confidence": query_language.confidence,
        "language_filter": effective_language,
        "match_query": match_query,
        "lexical_index_variant": "language_fts_v2" if match_query is not None and 'use_language_fts' in locals() and use_language_fts else "legacy_fts",
        "window_hit_count": len(rows),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
