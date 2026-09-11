from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.business_store import init_business_db, list_users
from service.drama_subtitle_task_executor import execute_claimed_drama_subtitle_task
from service.drama_subtitle_task_store import create_drama_subtitle_task, list_drama_subtitle_task_items


SUBTITLE_DB = ROOT_DIR / "data" / "drama_subtitle_similarity_v1.sqlite3"
ITEM_COUNT = 26


def _build_input(path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(SUBTITLE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT book_id, book_name, episode_order, time_start, time_end, window_text
              FROM drama_subtitle_windows
             WHERE length(window_text) BETWEEN 320 AND 500
             ORDER BY book_id, episode_order, line_start
             LIMIT ?
            """,
            (ITEM_COUNT,),
        ).fetchall()
    if len(rows) != ITEM_COUNT:
        raise RuntimeError(f"expected {ITEM_COUNT} subtitle windows, got {len(rows)}")
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "source_ref", "source_video_id", "source_channel", "source_upload_date",
                "source_caption_language", "source_caption_source", "source_segment_order",
                "source_time_start", "source_time_end", "source_display_title", "query_text",
                "source_text_original",
            ],
        )
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow({
                "source_ref": f"batch-fixture-video#segment-{index}",
                "source_video_id": f"batch-fixture-video-{(index - 1) // 5 + 1}",
                "source_channel": "existing subtitle fixture",
                "source_upload_date": "2026-08-03",
                "source_caption_language": "en",
                "source_caption_source": "fixture",
                "source_segment_order": index,
                "source_time_start": row["time_start"],
                "source_time_end": row["time_end"],
                "source_display_title": row["book_name"],
                "query_text": row["window_text"],
                "source_text_original": row["window_text"],
            })
    return rows


def _run_once(input_path: Path, business_db: Path, task_id: str) -> dict[str, object]:
    owner = list_users(business_db)[0]
    create_drama_subtitle_task(
        db_path=business_db,
        task_id=task_id,
        source_file_name=input_path.name,
        source_file_ext=input_path.suffix,
        source_file_path=str(input_path),
        source_file_sha256="benchmark-fixture",
        source_file_size=input_path.stat().st_size,
        owner_user_id=int(owner["user_id"]),
        created_by="benchmark",
        params={"top_k": 5, "window_limit": 50},
    )
    started = time.perf_counter()
    result = execute_claimed_drama_subtitle_task(
        task_id=task_id,
        business_db_path=business_db,
        subtitle_db_path=SUBTITLE_DB,
    )
    total_seconds = time.perf_counter() - started
    items = list_drama_subtitle_task_items(business_db, task_id)
    durations = [float(item["duration_seconds"] or 0.0) for item in items]
    matched = [item for item in items if item.get("matched_book_id")]
    return {
        "task_id": task_id,
        "status": result.get("status"),
        "input_count": len(items),
        "completed_count": sum(item.get("status") == "completed" for item in items),
        "failed_count": sum(item.get("status") == "failed" for item in items),
        "matched_count": len(matched),
        "total_seconds": round(total_seconds, 4),
        "avg_item_seconds": round(sum(durations) / len(durations), 4),
        "min_item_seconds": round(min(durations), 4),
        "max_item_seconds": round(max(durations), 4),
        "matched_book_ids": sorted({str(item["matched_book_id"]) for item in matched}),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="drama-subtitle-benchmark-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "benchmark_26_subtitle_segments.csv"
        source_rows = _build_input(input_path)
        business_db = root / "business.sqlite3"
        init_business_db(business_db)
        first = _run_once(input_path, business_db, "subtitle-benchmark-1")
        second = _run_once(input_path, business_db, "subtitle-benchmark-2")
        first_ids = first["matched_book_ids"]
        second_ids = second["matched_book_ids"]
        summary = {
            "source": str(SUBTITLE_DB),
            "input_count": len(source_rows),
            "rounds": [first, second],
            "stable_matched_book_set": first_ids == second_ids,
            "stable_status": first["status"] == second["status"] == "completed",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
