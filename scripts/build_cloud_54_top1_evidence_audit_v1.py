from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import openpyxl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a readable evidence audit for baseline IDs ranked Top1.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def normalize_id(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def load_rows(path: Path) -> list[dict[str, Any]]:
    worksheet = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    iterator = worksheet.iter_rows(values_only=True)
    headers = [str(value or "") for value in next(iterator)]
    return [dict(zip(headers, row)) for row in iterator if any(value not in (None, "") for value in row)]


def payload(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("result_payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    args = parse_args()
    sources = load_rows(Path(args.input))
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    items = {int(item["item_order"]): item for item in raw.get("items") or []}
    sections: list[str] = ["# 54 条重跑中正确原版 Top1 证据审查", ""]
    count = 0
    for order, source in enumerate(sources, start=1):
        baseline_id = normalize_id(source.get("基准原版剧ID"))
        item = items.get(order, {})
        data = payload(item)
        fallback = data.get("translation_fallback") if isinstance(data.get("translation_fallback"), dict) else {}
        selected_attempt: dict[str, Any] | None = None
        selected_candidate: dict[str, Any] | None = None
        for attempt in fallback.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            for index, candidate in enumerate(attempt.get("candidates") or [], start=1):
                if not isinstance(candidate, dict):
                    continue
                rank = int(candidate.get("rank") or index)
                if normalize_id(candidate.get("book_id")) == baseline_id and rank == 1:
                    selected_attempt = attempt
                    selected_candidate = candidate
                    break
            if selected_candidate is not None:
                break
        if selected_candidate is None or selected_attempt is None:
            continue
        count += 1
        same_book = [
            candidate
            for candidate in selected_attempt.get("candidates") or []
            if isinstance(candidate, dict) and normalize_id(candidate.get("book_id")) == baseline_id
        ]
        decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
        sections.extend(
            [
                f"## {order}. {source.get('视频 ID') or ''}",
                "",
                f"- 输入语言：`{item.get('query_language_code') or data.get('query_language_code') or ''}`；检索目标语言：`{selected_attempt.get('target_language_code') or ''}`",
                f"- 来源剧名：{source.get('来源剧名') or ''}",
                f"- 基准原版：`{baseline_id}`《{source.get('基准原版剧名称') or ''}》",
                f"- 系统判定：`{decision.get('outcome') or decision.get('status') or ''}`；判定 Book ID：`{normalize_id(decision.get('book_id'))}`",
                f"- Top1 语义分数：`{selected_candidate.get('semantic_score')}`；同一原版候选窗口数：`{len(same_book)}`",
                "",
                "### 输入翻译文本",
                "",
                "```text",
                str(selected_attempt.get("translated_text") or "")[:5000],
                "```",
                "",
                "### 原版字幕候选证据",
                "",
            ]
        )
        for candidate in same_book:
            sections.extend(
                [
                    f"候选排名 `{candidate.get('rank')}`，第 `{candidate.get('episode_order')}` 集，语义 `{candidate.get('semantic_score')}`，"
                    f"共享单元 `{candidate.get('shared_match_count')}`，查询覆盖 `{candidate.get('query_coverage_rate')}`。",
                    "",
                    "```text",
                    str(candidate.get("evidence_text") or ""),
                    "```",
                    "",
                ]
            )
    sections.insert(2, f"共提取 {count} 条基准原版 ID 排名第一的跨语言样本。")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")
    print(json.dumps({"top1_count": count, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
