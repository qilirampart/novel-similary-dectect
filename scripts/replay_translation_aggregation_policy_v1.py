from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.drama_subtitle_translation import (
    _content_candidate_options,
    _decision_with_alignment,
    _select_translation_review,
    _translation_alignment_review_decision,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the safe cross-language aggregation policy.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def replay_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(str(item.get("result_payload_json") or "{}"))
    old_decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    reviews: list[dict[str, Any]] = []
    attempts = (payload.get("translation_fallback") or {}).get("attempts")
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        candidates = [candidate for candidate in attempt.get("candidates") or [] if isinstance(candidate, dict)]
        attempt_payload = {"candidates": candidates}
        translated_text = str(attempt.get("translated_text") or "")
        legacy_decision = _decision_with_alignment(
            attempt.get("decision") if isinstance(attempt.get("decision"), dict) else {},
            attempt_payload,
            translated_text=translated_text,
        )
        alignment_decision = (
            _translation_alignment_review_decision(
                attempt_payload,
                translated_text=translated_text,
            )
            if str(legacy_decision.get("content_match_status") or "") != "matched"
            else None
        )
        decisions: list[dict[str, Any]] = []
        if str(legacy_decision.get("outcome") or "no_match") != "no_match":
            decisions.append(legacy_decision)
        if alignment_decision is not None:
            if str(alignment_decision.get("book_id") or "") == str(legacy_decision.get("book_id") or ""):
                decisions = [alignment_decision]
            else:
                decisions.append(alignment_decision)
        for decision in decisions:
            reviews.append(
                {
                    "translated_payload": attempt_payload,
                    "translated_decision": decision,
                    "translated": {},
                    "target_language": str(attempt.get("target_language_code") or ""),
                }
            )

    if reviews:
        selected = _select_translation_review(reviews)
        new_decision = selected["translated_decision"]
        options = _content_candidate_options(reviews)
    else:
        selected = None
        new_decision = {}
        options = []

    old_id = str(old_decision.get("book_id") or "")
    new_id = str(new_decision.get("book_id") or "")
    return {
        "item_order": item.get("item_order"),
        "source_video_id": item.get("source_video_id"),
        "source_display_title": item.get("source_display_title"),
        "query_language_code": item.get("query_language_code"),
        "old_outcome": old_decision.get("outcome"),
        "old_book_id": old_id,
        "old_book_name": old_decision.get("book_name"),
        "new_outcome": new_decision.get("outcome"),
        "new_book_id": new_id,
        "new_book_name": new_decision.get("book_name"),
        "new_reason": new_decision.get("reason"),
        "new_ordered_alignment_score": new_decision.get("ordered_alignment_score"),
        "new_content_candidate_options": options,
        "candidate_option_count": len(options),
        "book_id_changed": old_id != new_id,
        "from_no_match": not old_id and bool(new_id),
        "selected_target_language": selected.get("target_language") if selected else "",
    }


def main() -> int:
    args = parse_args()
    source = load_json(args.input)
    records = [replay_item(item) for item in source.get("items") or [] if isinstance(item, dict)]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "translation_aggregation_safe_policy_replay.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    changed = [record for record in records if record["book_id_changed"]]
    promoted = [record for record in records if record["from_no_match"]]
    guard_orders = {12, 17, 24, 44}
    lines = [
        "# 54条跨语言结果安全聚合策略 A/B 回放",
        "",
        "本报告只重放历史云端响应中的候选和翻译结果，不重新调用外部翻译、Embedding 或 Qdrant。旧结果取历史响应中的最终 decision，新结果使用当前本地安全聚合策略。Book ID 变化不等同于内容命中变化，最终仍需以字幕证据和内容候选为准。",
        "",
        "## 汇总",
        "",
        f"- 总样本：{len(records)} 条。",
        f"- 首选 Book ID 变化：{len(changed)} 条。该指标只表示聚合排序变化，不直接表示准确率变化。",
        f"- 从无 Book ID 提升为待复核候选：{len(promoted)} 条。",
        f"- 内容候选列表非空：{sum(record['candidate_option_count'] > 0 for record in records)} 条。",
        f"- 明确负样本保护集第 12、17、24、44 条中仍无候选：{sum(not records[order - 1]['new_book_id'] for order in guard_orders)} 条。第24条若历史已有待复核候选则保持，不按无候选计数。",
        "- 新策略不会把翻译候选自动确认为命中，仍统一落入待复核。",
        "",
        "## 变化明细",
        "",
        "| 序号 | 旧候选 | 新首选 | 目标语言 | 同序对齐 | 候选数 | 说明 |",
        "| ---: | --- | --- | --- | ---: | ---: | --- |",
    ]
    for record in changed:
        old_name = str(record.get("old_book_name") or record.get("old_book_id") or "未命中")
        new_name = str(record.get("new_book_name") or record.get("new_book_id") or "未命中")
        alignment = record.get("new_ordered_alignment_score")
        alignment_text = f"{float(alignment):.3f}" if isinstance(alignment, (int, float)) else "-"
        note = "从无候选提升" if record["from_no_match"] else "弱候选与强候选重新排序"
        lines.append(
            f"| {record['item_order']} | {old_name} | {new_name} | {record.get('selected_target_language') or '-'} | {alignment_text} | {record['candidate_option_count']} | {note} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "本轮只允许在旧候选缺少连续证据且新候选具有明显对齐优势时改变待复核首选；其他情况下保留旧候选，并把跨语言候选追加到内容候选列表。该结果可以作为本地 API 灰度前的基线，但不能替代人工金标集对内容命中率的验收。",
            "",
        ]
    )
    (args.output_dir / "translation_aggregation_safe_policy_replay.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    print(json.dumps({
        "records": len(records),
        "book_id_changed": len(changed),
        "promoted_from_no_match": len(promoted),
        "with_candidate_options": sum(record["candidate_option_count"] > 0 for record in records),
        "guard_no_book_id": sum(not records[order - 1]["new_book_id"] for order in guard_orders),
        "outcomes": dict(Counter(str(record.get("new_outcome") or "") for record in records)),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
