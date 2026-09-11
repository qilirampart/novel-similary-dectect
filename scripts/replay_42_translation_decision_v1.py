from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.drama_subtitle_translation import (
    _build_translation_assisted_result,
    _content_candidate_options,
    _decision_with_alignment,
    _select_translation_review,
    _translation_alignment_review_decision,
)


RAW = ROOT / "docs/42_coverage_rerun_20260902/task_b3b87390-1d43-4487-b3a3-225c1f0dbc74_raw.json"
OUT_DIR = ROOT / "docs/42_coverage_rerun_20260902"


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(str(item.get("result_payload_json") or "{}"))
    return value if isinstance(value, dict) else {}


def replay_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(item)
    old_decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    reviews: list[dict[str, Any]] = []
    attempts = payload.get("translation_fallback", {}).get("attempts", [])
    for attempt in attempts if isinstance(attempts, list) else []:
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
                    "translated": {
                        "text": translated_text,
                        "provider": str(attempt.get("provider") or "replay"),
                    },
                    "target_language": str(attempt.get("target_language_code") or ""),
                }
            )

    if not reviews:
        new_payload = payload
    else:
        ordered_reviews = sorted(
            reviews,
            key=lambda value: value["translated_decision"].get("semantic_score") or 0,
            reverse=True,
        )
        selected = _select_translation_review(ordered_reviews)
        new_payload = _build_translation_assisted_result(
            native_payload={
                "query_text": payload.get("query_text") or "",
                "query_language_code": payload.get("query_language_code") or "unknown",
                "query_language_confidence": payload.get("query_language_confidence") or 0.0,
            },
            translated_payload=selected["translated_payload"],
            translated_decision=selected["translated_decision"],
            translated=selected["translated"],
            fallback=payload.get("translation_fallback") or {},
            source_language=str(payload.get("query_language_code") or "unknown"),
            target_language=str(selected.get("target_language") or ""),
            content_candidate_options=_content_candidate_options(ordered_reviews),
        )

    new_decision = new_payload.get("decision") if isinstance(new_payload.get("decision"), dict) else {}
    return {
        "item_order": item.get("item_order"),
        "old_status": old_decision.get("status"),
        "old_outcome": old_decision.get("outcome"),
        "old_content_match_status": old_decision.get("content_match_status"),
        "new_status": new_decision.get("status"),
        "new_outcome": new_decision.get("outcome"),
        "new_content_match_status": new_decision.get("content_match_status"),
        "new_title_resolution": new_decision.get("title_resolution"),
        "new_reason": new_decision.get("reason"),
        "new_book_id": new_decision.get("book_id"),
        "new_book_name": new_decision.get("book_name"),
        "new_semantic_score": new_decision.get("semantic_score"),
        "new_alignment_score": new_decision.get("ordered_alignment_score"),
        "new_aligned_characters": new_decision.get("aligned_character_count"),
        "new_alignment_book_ids": new_decision.get("translation_alignment_book_ids") or [],
        "new_auto_confirmed": bool(new_decision.get("translation_alignment_auto_confirmed")),
    }


def main() -> None:
    source = json.loads(RAW.read_text(encoding="utf-8"))
    records = [replay_item(item) for item in source.get("items", []) if isinstance(item, dict)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "42条翻译判定策略离线回放.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    old_outcomes = Counter(str(item.get("old_outcome") or "") for item in records)
    new_outcomes = Counter(str(item.get("new_outcome") or "") for item in records)
    new_content = Counter(str(item.get("new_content_match_status") or "") for item in records)
    promoted = [item for item in records if item["new_auto_confirmed"]]
    ambiguous = [item for item in records if item["new_outcome"] == "content_matched_ambiguous"]
    lines = [
        "# 42条翻译判定策略离线 A/B 回放",
        "",
        "本报告仅使用 2026-09-02 本地重跑保存的翻译候选、证据和内部判定，不重新调用翻译 API、Embedding 或 Qdrant。A 版为历史最终输出，B 版使用当前代码的连续对白证据分层策略。Book ID 仅用于识别候选冲突，不作为命中真值。",
        "",
        "## 汇总",
        "",
        f"- 样本数：{len(records)} 条。",
        f"- 历史 outcome：{dict(old_outcomes)}。",
        f"- 新 outcome：{dict(new_outcomes)}。",
        f"- 新 content_match_status：{dict(new_content)}。",
        f"- 升级为自动确认命中：{len(promoted)} 条。",
        f"- 内容已命中但候选版本有歧义：{len(ambiguous)} 条。",
        "- 未修改原语言路径、全局语义阈值或候选召回流程；弱语义和短重合仍不能自动确认。",
        "",
        "## 自动确认",
        "",
        "| 序号 | 候选剧名 | 语义分数 | 同序对齐率 | 对齐字符 | 结论 |",
        "| ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for item in promoted:
        lines.append(
            f"| {item['item_order']} | {item.get('new_book_name') or item.get('new_book_id') or '-'} | "
            f"{float(item.get('new_semantic_score') or 0):.3f} | "
            f"{float(item.get('new_alignment_score') or 0):.3f} | "
            f"{item.get('new_aligned_characters') or 0} | 确认命中 |"
        )
    lines.extend(
        [
            "",
            "## 版本歧义",
            "",
            "| 序号 | 主候选 | 强证据 Book ID | 结论 |",
            "| ---: | --- | --- | --- |",
        ]
    )
    for item in ambiguous:
        lines.append(
            f"| {item['item_order']} | {item.get('new_book_name') or item.get('new_book_id') or '-'} | "
            f"{', '.join(item.get('new_alignment_book_ids') or [])} | 内容已命中，剧名或版本待确认 |"
        )
    lines.extend(
        [
            "",
            "## 安全边界",
            "",
            "自动确认必须同时满足高语义分数、较长同顺序对齐、足够长的证据窗口，并且候选 Book ID 不能出现其他同等级强证据。出现多个强证据版本时只提升内容状态，不提升公共命中状态；只有中等语义、短对齐、通用对白或没有连续证据时，仍保持待复核或未命中。",
            "",
        ]
    )
    (OUT_DIR / "42条翻译判定策略离线回放.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "records": len(records),
                "old_outcomes": dict(old_outcomes),
                "new_outcomes": dict(new_outcomes),
                "new_content_match_status": dict(new_content),
                "auto_confirmed": len(promoted),
                "content_matched_ambiguous": len(ambiguous),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
