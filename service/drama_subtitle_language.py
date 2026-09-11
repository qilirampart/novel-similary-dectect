from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Iterable


SUPPORTED_LANGUAGE_CODES = {"zh", "en", "ko", "ja"}
LATIN_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", flags=re.UNICODE)
PORTUGUESE_MARKERS = {
    "ainda",
    "alcateia",
    "agora",
    "alguém",
    "aqui",
    "como",
    "com",
    "daqui",
    "dele",
    "dela",
    "está",
    "estão",
    "filho",
    "filha",
    "minha",
    "meu",
    "muito",
    "não",
    "ninguém",
    "obrigado",
    "para",
    "pois",
    "porque",
    "pra",
    "seu",
    "sua",
    "também",
    "tem",
    "uma",
    "você",
    "vocês",
}


@dataclass(frozen=True)
class LanguagePrediction:
    language_code: str
    confidence: float


def _portuguese_confidence(text: str) -> float:
    words = [word.casefold() for word in LATIN_WORD_RE.findall(str(text or ""))]
    if len(words) < 8:
        return 0.0
    marker_count = sum(1 for word in words if word in PORTUGUESE_MARKERS)
    accented_count = sum(1 for word in words if any(char in word for char in "ãõáàâéêíóôúç"))
    if marker_count < 4 or marker_count + accented_count < 6:
        return 0.0
    support = (marker_count + min(accented_count, 8)) / max(len(words), 1)
    return round(min(0.99, 0.72 + support), 4)


def classify_subtitle_text(text: str) -> LanguagePrediction:
    counts: Counter[str] = Counter()
    for char in str(text or ""):
        codepoint = ord(char)
        if 0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF:
            counts["ja"] += 1
        elif 0xAC00 <= codepoint <= 0xD7AF:
            counts["ko"] += 1
        elif 0x4E00 <= codepoint <= 0x9FFF:
            counts["zh"] += 1
        elif ("A" <= char <= "Z") or ("a" <= char <= "z"):
            counts["en"] += 1

    if not counts:
        return LanguagePrediction("unknown", 0.0)
    if counts["ja"] > 0:
        # Kana disambiguates Japanese even when the line also includes Kanji.
        support = counts["ja"] + counts["zh"]
        return LanguagePrediction("ja", round(support / sum(counts.values()), 4))
    if counts["en"] > 0 and counts["en"] == max(counts.values()):
        portuguese_confidence = _portuguese_confidence(text)
        if portuguese_confidence > 0:
            return LanguagePrediction("pt", portuguese_confidence)
    language_code, count = counts.most_common(1)[0]
    return LanguagePrediction(language_code, round(count / sum(counts.values()), 4))


def aggregate_subtitle_languages(
    predictions: Iterable[tuple[str, float, int]],
) -> LanguagePrediction:
    votes: Counter[str] = Counter()
    for language_code, confidence, char_count in predictions:
        if language_code not in SUPPORTED_LANGUAGE_CODES:
            continue
        votes[language_code] += max(int(char_count), 1) * max(float(confidence), 0.1)
    if not votes:
        return LanguagePrediction("unknown", 0.0)

    language_code, score = votes.most_common(1)[0]
    total = sum(votes.values())
    confidence = score / total if total else 0.0
    if confidence < 0.72 and len(votes) > 1:
        return LanguagePrediction("mixed", round(confidence, 4))
    return LanguagePrediction(language_code, round(confidence, 4))


def ensure_language_columns(conn: Any) -> None:
    table_columns = {
        "drama_subtitle_lines": {
            "language_code": "TEXT NOT NULL DEFAULT 'unknown'",
            "language_confidence": "REAL NOT NULL DEFAULT 0",
        },
        "drama_subtitle_windows": {
            "language_code": "TEXT NOT NULL DEFAULT 'unknown'",
            "language_confidence": "REAL NOT NULL DEFAULT 0",
        },
    }
    existing_tables: set[str] = set()
    for table_name, required_columns in table_columns.items():
        existing = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if not existing:
            continue
        existing_tables.add(table_name)
        for column_name, column_type in required_columns.items():
            if column_name not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    if "drama_subtitle_lines" in existing_tables:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_drama_subtitle_lines_language_episode
                ON drama_subtitle_lines (language_code, episode_uid, line_order)
            """
        )
    if "drama_subtitle_windows" in existing_tables:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_drama_subtitle_windows_language_book_episode
                ON drama_subtitle_windows (language_code, book_id, episode_order, line_start)
            """
        )
