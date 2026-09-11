from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from v2_common import connect_db, now_ts
from service.drama_subtitle_language import (
    aggregate_subtitle_languages,
    ensure_language_columns,
)


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"
DEFAULT_BASE_SCHEMA_PATH = "service/drama_subtitle_schema_v1.sql"
DEFAULT_WINDOW_SCHEMA_PATH = "service/drama_subtitle_windows_schema_v1.sql"
DEFAULT_OUTPUT_ROOT = "data_samples/middle_platform_subtitles_20260722_phase2"
DEFAULT_LINE_WINDOW_SIZE = 16
DEFAULT_LINE_OVERLAP = 6
DEFAULT_MIN_WINDOW_LINES = 6
DEFAULT_MAX_CHAR_COUNT = 500
DEFAULT_PREVIEW_CHARS = 180
DEFAULT_COMMIT_EVERY = 400


@dataclass(frozen=True)
class EpisodeMeta:
    episode_uid: str
    book_id: str
    book_name: str
    episode_order: int
    chapter_id: int | None
    real_subtitle_line_count: int


@dataclass(frozen=True)
class SubtitleLine:
    line_order: int
    start_time: str
    end_time: str
    text_normalized: str
    language_code: str
    language_confidence: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build sliding subtitle windows from normalized drama subtitle lines.",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--base-schema", default=DEFAULT_BASE_SCHEMA_PATH)
    parser.add_argument("--window-schema", default=DEFAULT_WINDOW_SCHEMA_PATH)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--book-id", default="", help="Only process one target book id.")
    parser.add_argument("--book-ids-file", default="", help="Only process newline-delimited target book ids.")
    parser.add_argument("--limit-episodes", type=int, default=0, help="Only process first N episodes.")
    parser.add_argument("--line-window-size", type=int, default=DEFAULT_LINE_WINDOW_SIZE)
    parser.add_argument("--line-overlap", type=int, default=DEFAULT_LINE_OVERLAP)
    parser.add_argument("--min-window-lines", type=int, default=DEFAULT_MIN_WINDOW_LINES)
    parser.add_argument("--max-char-count", type=int, default=DEFAULT_MAX_CHAR_COUNT)
    parser.add_argument("--preview-chars", type=int, default=DEFAULT_PREVIEW_CHARS)
    parser.add_argument("--commit-every", type=int, default=DEFAULT_COMMIT_EVERY)
    parser.add_argument("--overwrite", action="store_true", help="Reset existing subtitle windows first.")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_book_ids_file(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    }


def validate_args(args: argparse.Namespace) -> None:
    if args.line_window_size <= 0:
        raise ValueError("--line-window-size must be > 0")
    if args.line_overlap < 0:
        raise ValueError("--line-overlap must be >= 0")
    if args.line_overlap >= args.line_window_size:
        raise ValueError("--line-overlap must be < --line-window-size")
    if args.min_window_lines <= 0:
        raise ValueError("--min-window-lines must be > 0")
    if args.max_char_count <= 0:
        raise ValueError("--max-char-count must be > 0")
    if args.preview_chars <= 0:
        raise ValueError("--preview-chars must be > 0")
    if args.commit_every <= 0:
        raise ValueError("--commit-every must be > 0")


def build_preview(text: str, preview_chars: int) -> str:
    if len(text) <= preview_chars:
        return text
    return text[:preview_chars].rstrip() + "..."


def load_episode_metas(
    conn,
    *,
    book_id: str = "",
    book_ids_file: bool = False,
    limit_episodes: int = 0,
) -> list[EpisodeMeta]:  # type: ignore[no-untyped-def]
    sql = """
        SELECT e.episode_uid,
               e.book_id,
               b.book_name,
               e.episode_order,
               e.chapter_id,
               e.real_subtitle_line_count
          FROM drama_episodes e
          JOIN drama_books b ON b.book_id = e.book_id
         WHERE e.real_subtitle_line_count > 0
    """
    params: list[object] = []
    if book_id:
        sql += " AND e.book_id = ?"
        params.append(book_id)
    elif book_ids_file:
        sql += " AND e.book_id IN (SELECT book_id FROM temp_target_book_ids)"
    sql += " ORDER BY e.book_id, e.episode_order"
    if limit_episodes > 0:
        sql += f" LIMIT {limit_episodes}"

    rows = conn.execute(sql, params).fetchall()
    return [
        EpisodeMeta(
            episode_uid=str(row[0]),
            book_id=str(row[1]),
            book_name=str(row[2]),
            episode_order=int(row[3]),
            chapter_id=int(row[4]) if row[4] is not None else None,
            real_subtitle_line_count=int(row[5]),
        )
        for row in rows
    ]


def load_episode_lines(conn, episode_uid: str) -> list[SubtitleLine]:  # type: ignore[no-untyped-def]
    rows = conn.execute(
        """
        SELECT line_order, start_time, end_time, text_normalized, language_code, language_confidence
          FROM drama_subtitle_lines
         WHERE episode_uid = ?
         ORDER BY line_order
        """,
        (episode_uid,),
    ).fetchall()
    return [
        SubtitleLine(
            line_order=int(row[0]),
            start_time=str(row[1] or ""),
            end_time=str(row[2] or ""),
            text_normalized=str(row[3] or ""),
            language_code=str(row[4] or "unknown"),
            language_confidence=float(row[5] or 0.0),
        )
        for row in rows
        if str(row[3] or "").strip()
    ]


def build_start_indexes(total_lines: int, *, line_window_size: int, line_overlap: int) -> list[int]:
    if total_lines <= 0:
        return []
    if total_lines <= line_window_size:
        return [0]

    step = max(1, line_window_size - line_overlap)
    last_start = max(total_lines - line_window_size, 0)
    starts = list(range(0, last_start + 1, step))
    if not starts or starts[-1] != last_start:
        starts.append(last_start)
    return starts


def materialize_window(
    lines: list[SubtitleLine],
    start_index: int,
    *,
    line_window_size: int,
    min_window_lines: int,
    max_char_count: int,
) -> tuple[int, str]:
    total_lines = len(lines)
    end_limit = min(total_lines, start_index + line_window_size)
    parts: list[str] = []
    char_count = 0
    cursor = start_index

    while cursor < end_limit:
        piece = lines[cursor].text_normalized.strip()
        if not piece:
            cursor += 1
            continue

        added_chars = len(piece) + (1 if parts else 0)
        current_line_count = cursor - start_index
        should_stop = char_count + added_chars > max_char_count and current_line_count >= min_window_lines
        if should_stop:
            break

        parts.append(piece)
        char_count += added_chars
        cursor += 1

    if not parts:
        first_piece = lines[start_index].text_normalized.strip()
        return start_index + 1, first_piece

    return cursor, "\n".join(parts)


def build_episode_windows(
    episode: EpisodeMeta,
    lines: list[SubtitleLine],
    *,
    line_window_size: int,
    line_overlap: int,
    min_window_lines: int,
    max_char_count: int,
    preview_chars: int,
) -> list[tuple[object, ...]]:
    if not lines:
        return []

    created_at = now_ts()
    starts = build_start_indexes(
        len(lines),
        line_window_size=line_window_size,
        line_overlap=line_overlap,
    )
    seen_segments: set[tuple[int, int]] = set()
    rows: list[tuple[object, ...]] = []

    for start_index in starts:
        end_exclusive, text = materialize_window(
            lines,
            start_index,
            line_window_size=line_window_size,
            min_window_lines=min_window_lines,
            max_char_count=max_char_count,
        )
        if end_exclusive <= start_index:
            continue

        line_start = lines[start_index].line_order
        line_end = lines[end_exclusive - 1].line_order
        segment = (line_start, line_end)
        if segment in seen_segments:
            continue
        seen_segments.add(segment)

        time_start = lines[start_index].start_time
        time_end = lines[end_exclusive - 1].end_time
        line_count = end_exclusive - start_index
        preview = build_preview(text, preview_chars)
        window_uid = f"{episode.episode_uid}:{line_start}-{line_end}"
        language = aggregate_subtitle_languages(
            (
                line.language_code,
                line.language_confidence,
                len(line.text_normalized),
            )
            for line in lines[start_index:end_exclusive]
        )
        rows.append(
            (
                window_uid,
                episode.book_id,
                episode.book_name,
                episode.episode_uid,
                episode.episode_order,
                episode.chapter_id,
                line_start,
                line_end,
                time_start,
                time_end,
                text,
                preview,
                line_count,
                len(text),
                language.language_code,
                language.confidence,
                created_at,
            )
        )

    return rows


def reset_windows(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute("DELETE FROM drama_subtitle_windows")
    conn.commit()


def flush_windows(
    conn,  # type: ignore[no-untyped-def]
    *,
    episode_ids: list[str],
    window_rows: list[tuple[object, ...]],
    delete_existing: bool,
) -> None:
    if not window_rows:
        return

    conn.execute("BEGIN")
    if delete_existing and episode_ids:
        conn.executemany(
            "DELETE FROM drama_subtitle_windows WHERE episode_uid = ?",
            [(episode_uid,) for episode_uid in episode_ids],
        )
    conn.executemany(
        """
        INSERT INTO drama_subtitle_windows(
            window_uid,
            book_id,
            book_name,
            episode_uid,
            episode_order,
            chapter_id,
            line_start,
            line_end,
            time_start,
            time_end,
            window_text,
            window_text_preview,
            line_count,
            char_count,
            language_code,
            language_confidence,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        window_rows,
    )
    conn.commit()


def write_episode_summary_csv(path: Path, rows: list[dict[str, object]]) -> None:
    ensure_dir(path.parent)
    fieldnames = [
        "book_id",
        "book_name",
        "episode_uid",
        "episode_order",
        "real_subtitle_line_count",
        "window_count",
        "min_window_line",
        "max_window_line",
        "max_window_char_count",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary_json(path: Path, payload: dict[str, object]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    validate_args(args)

    db_path = resolve_path(args.db)
    base_schema_path = resolve_path(args.base_schema)
    window_schema_path = resolve_path(args.window_schema)
    output_root = resolve_path(args.output_root)
    ensure_dir(output_root)

    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")
    if not base_schema_path.exists():
        raise FileNotFoundError(f"base schema not found: {base_schema_path}")
    if not window_schema_path.exists():
        raise FileNotFoundError(f"window schema not found: {window_schema_path}")

    book_id_file = resolve_path(args.book_ids_file) if args.book_ids_file.strip() else None
    if args.book_id.strip() and book_id_file:
        raise ValueError("--book-id and --book-ids-file cannot be used together")
    if book_id_file and not book_id_file.exists():
        raise FileNotFoundError(f"book ids file not found: {book_id_file}")
    target_book_ids = load_book_ids_file(book_id_file) if book_id_file else set()

    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.executescript(load_sql(base_schema_path))
        conn.executescript(load_sql(window_schema_path))
        ensure_language_columns(conn)
        if target_book_ids:
            conn.execute("CREATE TEMP TABLE temp_target_book_ids (book_id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO temp_target_book_ids(book_id) VALUES (?)",
                [(book_id,) for book_id in sorted(target_book_ids)],
            )
            conn.commit()
        if args.overwrite:
            reset_windows(conn)

        episodes = load_episode_metas(
            conn,
            book_id=args.book_id.strip(),
            book_ids_file=bool(target_book_ids),
            limit_episodes=max(int(args.limit_episodes or 0), 0),
        )

        started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        processed_episodes = 0
        processed_books: set[str] = set()
        inserted_windows = 0
        episodes_without_lines = 0
        episode_summary_rows: list[dict[str, object]] = []
        pending_episode_ids: list[str] = []
        pending_window_rows: list[tuple[object, ...]] = []

        for episode in episodes:
            lines = load_episode_lines(conn, episode.episode_uid)
            processed_episodes += 1
            processed_books.add(episode.book_id)
            if not lines:
                episodes_without_lines += 1
                continue

            rows = build_episode_windows(
                episode,
                lines,
                line_window_size=args.line_window_size,
                line_overlap=args.line_overlap,
                min_window_lines=args.min_window_lines,
                max_char_count=args.max_char_count,
                preview_chars=args.preview_chars,
            )
            pending_episode_ids.append(episode.episode_uid)
            pending_window_rows.extend(rows)
            inserted_windows += len(rows)

            episode_summary_rows.append(
                {
                    "book_id": episode.book_id,
                    "book_name": episode.book_name,
                    "episode_uid": episode.episode_uid,
                    "episode_order": episode.episode_order,
                    "real_subtitle_line_count": episode.real_subtitle_line_count,
                    "window_count": len(rows),
                    "min_window_line": rows[0][6] if rows else 0,
                    "max_window_line": rows[-1][7] if rows else 0,
                    "max_window_char_count": max((int(row[13]) for row in rows), default=0),
                }
            )

            if len(pending_episode_ids) >= args.commit_every:
                flush_windows(
                    conn,
                    episode_ids=pending_episode_ids,
                    window_rows=pending_window_rows,
                    delete_existing=not args.overwrite,
                )
                print(
                    f"PROGRESS processed_episodes={processed_episodes} "
                    f"processed_books={len(processed_books)} "
                    f"inserted_windows={inserted_windows}"
                )
                pending_episode_ids = []
                pending_window_rows = []

        flush_windows(
            conn,
            episode_ids=pending_episode_ids,
            window_rows=pending_window_rows,
            delete_existing=not args.overwrite,
        )

        finished_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_dir = output_root / "logs"
        summary_csv_path = logs_dir / f"phase2_episode_window_summary_{run_stamp}.csv"
        summary_json_path = logs_dir / f"phase2_build_summary_{run_stamp}.json"
        write_episode_summary_csv(summary_csv_path, episode_summary_rows)
        write_summary_json(
            summary_json_path,
            {
                "started_at": started_at,
                "finished_at": finished_at,
                "db_path": str(db_path),
                "processed_books": len(processed_books),
                "processed_episodes": processed_episodes,
                "inserted_windows": inserted_windows,
                "episodes_without_lines": episodes_without_lines,
                "line_window_size": args.line_window_size,
                "line_overlap": args.line_overlap,
                "min_window_lines": args.min_window_lines,
                "max_char_count": args.max_char_count,
                "preview_chars": args.preview_chars,
                "commit_every": args.commit_every,
                "overwrite": int(args.overwrite),
                "book_filter": args.book_id.strip(),
                "book_ids_file": str(book_id_file) if book_id_file else "",
                "target_book_id_count": len(target_book_ids),
                "limit_episodes": max(int(args.limit_episodes or 0), 0),
                "summary_csv_path": str(summary_csv_path),
            },
        )
    finally:
        conn.close()

    print(f"OK processed_books={len(processed_books)}")
    print(f"OK processed_episodes={processed_episodes}")
    print(f"OK inserted_windows={inserted_windows}")
    print(f"OK episodes_without_lines={episodes_without_lines}")
    print(f"OK line_window_size={args.line_window_size}")
    print(f"OK line_overlap={args.line_overlap}")
    print(f"OK min_window_lines={args.min_window_lines}")
    print(f"OK max_char_count={args.max_char_count}")
    print(f"OK db_path={db_path}")


if __name__ == "__main__":
    main()
