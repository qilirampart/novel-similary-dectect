from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import openpyxl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a content audit for review-required Top1 subtitle candidates.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def text(value: Any) -> str:
    return str(value or "").strip()


def normalize_id(value: Any) -> str:
    value_text = text(value)
    return value_text[:-2] if value_text.endswith(".0") and value_text[:-2].isdigit() else value_text


def load_rows(path: Path) -> list[dict[str, Any]]:
    worksheet = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    iterator = worksheet.iter_rows(values_only=True)
    headers = [text(value) for value in next(iterator)]
    return [
        dict(zip(headers, row))
        for row in iterator
        if any(value not in (None, "") for value in row)
    ]


def payload(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("result_payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(text(raw) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def candidate_evidence(candidate: dict[str, Any]) -> str:
    evidence = candidate.get("evidence")
    if isinstance(evidence, dict):
        return text(evidence.get("window_text") or evidence.get("text") or evidence.get("candidate_text"))
    return text(candidate.get("evidence_text") or candidate.get("candidate_text"))


def first_candidate(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate, text(data.get("translation_target_language_code"))
    fallback = data.get("translation_fallback")
    attempts = fallback.get("attempts") if isinstance(fallback, dict) else []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        for candidate in attempt.get("candidates") or []:
            if isinstance(candidate, dict):
                return candidate, text(attempt.get("target_language_code"))
    return {}, ""


def candidate_line(candidate: dict[str, Any]) -> str:
    metrics = candidate.get("match_metrics") if isinstance(candidate.get("match_metrics"), dict) else {}
    sources = ", ".join(text(value) for value in candidate.get("retrieval_sources") or [])
    return (
        f"Book ID `{normalize_id(candidate.get('book_id'))}`,《{text(candidate.get('book_name'))}》，"
        f"第 {text(candidate.get('episode_order'))} 集；语言 `{text(candidate.get('language_code'))}`；"
        f"语义 `{text(candidate.get('semantic_score'))}`；来源 `{sources}`；"
        f"词法共享 `{text(metrics.get('shared_trigram_count'))}`；"
        f"查询覆盖 `{text(metrics.get('query_coverage_rate'))}`；"
        f"同序对齐 `{text(candidate.get('ordered_alignment_score'))}`。"
    )


def main() -> int:
    args = parse_args()
    sources = load_rows(Path(args.input))
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    items = {int(item["item_order"]): item for item in raw.get("items") or []}
    sections = [
        "# 54 条云端重跑待复核 Top1 内容核对",
        "",
        "本表只包含最终状态为 `review_required` 的记录。候选存在不代表命中，需对照输入字幕和候选原字幕判断剧情是否对应。",
        "",
    ]
    count = 0
    for order, source in enumerate(sources, start=1):
        item = items.get(order, {})
        data = payload(item)
        decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
        if text(decision.get("status")) != "review_required":
            continue
        count += 1
        candidate, target_language = first_candidate(data)
        fallback = data.get("translation_fallback") if isinstance(data.get("translation_fallback"), dict) else {}
        sections.extend(
            [
                f"## {order}. {text(source.get('视频 ID'))}",
                "",
                f"- 基准原版：`{normalize_id(source.get('基准原版剧ID'))}`《{text(source.get('基准原版剧名称'))}》",
                f"- 输入语言：`{text(item.get('query_language_code') or data.get('query_language_code'))}`；Top1 检索语言：`{target_language}`",
                f"- 最终判定：`{text(decision.get('status'))}`；原因：`{text(decision.get('reason'))}`；判定候选：`{normalize_id(decision.get('book_id'))}`《{text(decision.get('book_name'))}》",
                f"- 候选是否确认命中：`{text(decision.get('is_confirmed_match'))}`；Top1：{candidate_line(candidate) if candidate else '无候选'}",
                f"- 翻译回退状态：`{text(fallback.get('status'))}`；用户提示：{text(decision.get('user_message'))}",
                "",
                "### 原始输入字幕",
                "",
                "```text",
                text(data.get("query_text") or item.get("source_text_original")),
                "```",
                "",
                "### 翻译文本",
                "",
                "```text",
                text(data.get("translated_query_text")),
                "```",
                "",
                "### 最终 Top1 候选原字幕证据",
                "",
                "```text",
                candidate_evidence(candidate),
                "```",
                "",
            ]
        )
    sections.insert(3, f"共 {count} 条待复核记录。")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")
    print(json.dumps({"review_count": count, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
