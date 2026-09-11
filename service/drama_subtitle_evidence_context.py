from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.v2_common import connect_db


DEFAULT_CONTEXT_CHARS = 2400
DEFAULT_BEFORE_LINES = 6
MAX_CONTEXT_CHARS = 5000
MAX_SCAN_LINES = 240


class DramaSubtitleEvidenceContextError(RuntimeError):
    pass


def get_drama_subtitle_evidence_context(
    *,
    db_path: str | Path,
    window_uid: str,
    context_chars: int = DEFAULT_CONTEXT_CHARS,
    before_lines: int = DEFAULT_BEFORE_LINES,
) -> dict[str, Any]:
    """Expand an indexed hit into display-only review context."""
    resolved_db_path = Path(db_path)
    if not resolved_db_path.exists():
        raise DramaSubtitleEvidenceContextError(f"subtitle database not found: {resolved_db_path}")
    normalized_uid = str(window_uid or "").strip()
    if not normalized_uid:
        raise ValueError("window_uid is empty")
    if not 600 <= context_chars <= MAX_CONTEXT_CHARS:
        raise ValueError(f"context_chars must be between 600 and {MAX_CONTEXT_CHARS}")
    if not 0 <= before_lines <= 30:
        raise ValueError("before_lines must be between 0 and 30")

    conn = connect_db(resolved_db_path)
    try:
        window = conn.execute(
            """
            SELECT window_uid, book_id, book_name, episode_uid, episode_order,
                   line_start, line_end, time_start, time_end, language_code
              FROM drama_subtitle_windows
             WHERE window_uid = ?
            """,
            (normalized_uid,),
        ).fetchone()
        if window is None:
            raise DramaSubtitleEvidenceContextError("subtitle evidence window not found")
        line_start = int(window[5])
        line_end = int(window[6])
        context_start = max(1, line_start - before_lines)
        lines = conn.execute(
            """
            SELECT line_order, start_time, end_time, text_normalized
              FROM drama_subtitle_lines
             WHERE episode_uid = ? AND line_order >= ?
             ORDER BY line_order ASC
             LIMIT ?
            """,
            (str(window[3]), context_start, MAX_SCAN_LINES),
        ).fetchall()
    finally:
        conn.close()

    if not lines:
        raise DramaSubtitleEvidenceContextError("original subtitle lines are unavailable for this evidence window")

    selected_lines: list[dict[str, Any]] = []
    text_parts: list[str] = []
    char_count = 0
    for row in lines:
        line_order = int(row[0])
        text = str(row[3] or "").strip()
        if not text:
            continue
        added_chars = len(text) + (1 if text_parts else 0)
        # Keep the indexed hit intact; the cap only limits post-hit context.
        if line_order > line_end and char_count + added_chars > context_chars:
            break
        selected_lines.append(
            {
                "line_order": line_order,
                "time_start": str(row[1] or ""),
                "time_end": str(row[2] or ""),
                "text": text,
                "is_hit_line": line_start <= line_order <= line_end,
            }
        )
        text_parts.append(text)
        char_count += added_chars

    if not selected_lines:
        raise DramaSubtitleEvidenceContextError("no displayable subtitle text found for this evidence window")

    last_line_order = int(selected_lines[-1]["line_order"])
    has_more_after = last_line_order < int(lines[-1][0]) or len(lines) == MAX_SCAN_LINES
    return {
        "window_uid": str(window[0]),
        "book_id": str(window[1]),
        "book_name": str(window[2]),
        "episode_uid": str(window[3]),
        "episode_order": int(window[4]),
        "language_code": str(window[9] or "unknown"),
        "hit": {
            "line_start": line_start,
            "line_end": line_end,
            "time_start": str(window[7] or ""),
            "time_end": str(window[8] or ""),
        },
        "context": {
            "line_start": int(selected_lines[0]["line_order"]),
            "line_end": last_line_order,
            "time_start": str(selected_lines[0]["time_start"]),
            "time_end": str(selected_lines[-1]["time_end"]),
            "line_count": len(selected_lines),
            "char_count": char_count,
            "before_line_count": sum(1 for line in selected_lines if int(line["line_order"]) < line_start),
            "after_line_count": sum(1 for line in selected_lines if int(line["line_order"]) > line_end),
            "has_more_after": has_more_after,
            "text": "\n".join(text_parts),
            "lines": selected_lines,
        },
    }
