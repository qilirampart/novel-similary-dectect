from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from v2_common import connect_db


DEFAULT_DB_PATH = "data/drama_subtitle_similarity_v1.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify drama subtitle text by its dominant written script."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def script_counts(text: str) -> dict[str, int]:
    counts = Counter()
    for char in text:
        codepoint = ord(char)
        if 0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF:
            counts["japanese_kana"] += 1
        elif 0xAC00 <= codepoint <= 0xD7AF:
            counts["korean_hangul"] += 1
        elif 0x0600 <= codepoint <= 0x06FF:
            counts["arabic"] += 1
        elif 0x0400 <= codepoint <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x4E00 <= codepoint <= 0x9FFF:
            counts["han"] += 1
        elif ("A" <= char <= "Z") or ("a" <= char <= "z"):
            counts["latin"] += 1
        elif not char.isspace() and not char.isdigit() and char.isprintable():
            counts["other"] += 1
    return dict(counts)


def classify_line(counts: dict[str, int]) -> str:
    # Kana is checked first because Japanese text often mixes Kana and Han characters.
    if counts.get("japanese_kana", 0) > 0:
        return "Japanese-script"
    if counts.get("korean_hangul", 0) > 0:
        return "Korean-script"
    if counts.get("arabic", 0) > 0:
        return "Arabic-script"
    if counts.get("cyrillic", 0) > 0:
        return "Cyrillic-script"
    if counts.get("han", 0) > 0:
        return "Chinese-script"
    if counts.get("latin", 0) > 0:
        return "Latin-script"
    return "Other-or-symbol-only"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    if args.sample_limit < 0:
        raise ValueError("--sample-limit must be >= 0")
    db_path = resolve_path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")

    line_counts: Counter[str] = Counter()
    character_counts: Counter[str] = Counter()
    script_character_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    conn = connect_db(db_path)
    conn.row_factory = None
    try:
        rows = conn.execute(
            """
            SELECT book_id, episode_order, line_order, text_normalized
              FROM drama_subtitle_lines
             WHERE text_normalized IS NOT NULL
               AND text_normalized != ''
            """
        )
        for book_id, episode_order, line_order, text in rows:
            text_value = str(text)
            counts = script_counts(text_value)
            label = classify_line(counts)
            line_counts[label] += 1
            character_counts[label] += sum(counts.values())
            script_character_counts.update(counts)
            if label != "Chinese-script" and len(samples[label]) < args.sample_limit:
                samples[label].append(
                    {
                        "book_id": str(book_id),
                        "episode_order": int(episode_order),
                        "line_order": int(line_order),
                        "text": text_value,
                    }
                )
    finally:
        conn.close()

    payload = {
        "db_path": str(db_path),
        "classification_note": (
            "This is written-script detection, not spoken-language detection. "
            "Japanese lines containing only Han characters can be counted as Chinese-script."
        ),
        "total_nonempty_subtitle_lines": sum(line_counts.values()),
        "line_counts": dict(sorted(line_counts.items())),
        "character_counts": dict(sorted(character_counts.items())),
        "script_character_counts": dict(sorted(script_character_counts.items())),
        "non_chinese_samples": dict(sorted(samples.items())),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        output_path = resolve_path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
