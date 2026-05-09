from __future__ import annotations

from pathlib import Path
from typing import Any
import sys


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_evidence_windows_v2 import iter_windows  # noqa: E402
from v2_common import connect_db  # noqa: E402


def normalize_chapter_uids(chapter_uids: list[int] | tuple[int, ...]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for value in chapter_uids:
        chapter_uid = int(value)
        if chapter_uid <= 0 or chapter_uid in seen:
            continue
        seen.add(chapter_uid)
        normalized.append(chapter_uid)
    return normalized


def chunked(values: list[int], chunk_size: int) -> list[list[int]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    return [values[idx : idx + chunk_size] for idx in range(0, len(values), chunk_size)]


def slice_text_windows(text: str, window_size: int = 200, step_size: int = 50) -> list[dict[str, Any]]:
    if window_size <= 0:
        raise ValueError("window_size must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")

    windows = iter_windows(text, window_size=window_size, step_size=step_size)
    return [
        {
            "window_order": int(window_order),
            "start_offset": int(start_offset),
            "end_offset": int(end_offset),
            "text": content,
            "char_count": len(content),
        }
        for window_order, start_offset, end_offset, content in windows
    ]


def fetch_candidate_chapter_texts(
    conn: Any,
    chapter_uids: list[int] | tuple[int, ...],
    batch_size: int = 500,
) -> dict[int, str]:
    normalized = normalize_chapter_uids(chapter_uids)
    if not normalized:
        return {}

    text_map: dict[int, str] = {}
    for batch in chunked(normalized, batch_size):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT chapter_uid,
                   COALESCE(NULLIF(content_clean, ''), NULLIF(content_retrieval, '')) AS content
              FROM chapter_contents
             WHERE chapter_uid IN ({placeholders})
            """,
            batch,
        ).fetchall()
        for chapter_uid, content in rows:
            if content:
                text_map[int(chapter_uid)] = str(content)
    return text_map


def build_candidate_windows(
    chapter_text_map: dict[int, str],
    window_size: int = 200,
    step_size: int = 50,
) -> dict[int, dict[str, Any]]:
    payload: dict[int, dict[str, Any]] = {}
    for chapter_uid, text in chapter_text_map.items():
        windows = slice_text_windows(text=text, window_size=window_size, step_size=step_size)
        payload[int(chapter_uid)] = {
            "chapter_uid": int(chapter_uid),
            "char_count": len(text),
            "window_count": len(windows),
            "windows": windows,
        }
    return payload


def slice_candidate_chapters(
    db_path: str,
    chapter_uids: list[int] | tuple[int, ...],
    window_size: int = 200,
    step_size: int = 50,
    batch_size: int = 500,
) -> dict[int, dict[str, Any]]:
    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        chapter_text_map = fetch_candidate_chapter_texts(
            conn=conn,
            chapter_uids=chapter_uids,
            batch_size=batch_size,
        )
    finally:
        conn.close()

    return build_candidate_windows(
        chapter_text_map=chapter_text_map,
        window_size=window_size,
        step_size=step_size,
    )
