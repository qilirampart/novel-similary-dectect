from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import openpyxl


CJK_LANGUAGES = {"zh", "ja", "ko"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit non-Top1 baseline dramas against local subtitle evidence.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--db", required=True)
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


def units(text: str, language: str) -> set[str]:
    if language in CJK_LANGUAGES:
        normalized = re.sub(r"\s+", "", text).lower()
        return {normalized[index : index + 3] for index in range(max(0, len(normalized) - 2))}
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(words[index : index + 4]) for index in range(max(0, len(words) - 3))}


def best_baseline_windows(
    connection: sqlite3.Connection,
    *,
    book_id: str,
    language: str,
    query_text: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    query_units = units(query_text, language)
    rows = connection.execute(
        """
        SELECT window_uid, episode_order, language_code, window_text
          FROM drama_subtitle_windows
         WHERE book_id = ? AND language_code = ? AND episode_order <= 10
         ORDER BY episode_order, line_start
        """,
        (book_id, language),
    ).fetchall()
    scored: list[dict[str, Any]] = []
    for window_uid, episode_order, language_code, window_text in rows:
        evidence_units = units(str(window_text or ""), language)
        shared = query_units & evidence_units
        scored.append(
            {
                "window_uid": window_uid,
                "episode_order": episode_order,
                "language_code": language_code,
                "shared_count": len(shared),
                "query_coverage": len(shared) / max(len(query_units), 1),
                "evidence_coverage": len(shared) / max(len(evidence_units), 1),
                "window_text": window_text,
            }
        )
    return sorted(
        scored,
        key=lambda row: (row["shared_count"], row["evidence_coverage"], row["query_coverage"]),
        reverse=True,
    )[:limit]


def find_rank(data: dict[str, Any], book_id: str) -> int | None:
    ranks: list[int] = []
    for attempt in (data.get("translation_fallback") or {}).get("attempts") or []:
        for index, candidate in enumerate(attempt.get("candidates") or [], start=1):
            if isinstance(candidate, dict) and normalize_id(candidate.get("book_id")) == book_id:
                ranks.append(int(candidate.get("rank") or index))
    for index, candidate in enumerate(data.get("candidates") or [], start=1):
        if isinstance(candidate, dict) and normalize_id(candidate.get("book_id")) == book_id:
            ranks.append(int(candidate.get("rank") or index))
    return min(ranks) if ranks else None


def main() -> int:
    args = parse_args()
    sources = load_rows(Path(args.input))
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    items = {int(item["item_order"]): item for item in raw.get("items") or []}
    connection = sqlite3.connect(args.db)
    sections = ["# 54 条重跑中非 Top1 跨语言样本语义复核", ""]
    count = 0
    try:
        for order, source in enumerate(sources, start=1):
            item = items.get(order, {})
            data = payload(item)
            query_language = str(item.get("query_language_code") or data.get("query_language_code") or "")
            if query_language == "zh":
                continue
            baseline_id = normalize_id(source.get("基准原版剧ID"))
            baseline_rank = find_rank(data, baseline_id)
            if baseline_rank == 1:
                continue
            count += 1
            decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
            languages = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT language_code FROM drama_subtitle_windows WHERE book_id = ? ORDER BY language_code",
                    (baseline_id,),
                ).fetchall()
            ]
            attempts = (data.get("translation_fallback") or {}).get("attempts") or []
            translated_by_language = {
                str(attempt.get("target_language_code") or ""): str(attempt.get("translated_text") or "")
                for attempt in attempts
                if isinstance(attempt, dict)
            }
            if query_language not in translated_by_language:
                translated_by_language[query_language] = str(data.get("query_text") or item.get("query_text") or "")
            sections.extend(
                [
                    f"## {order}. {item.get('source_video_id') or source.get('视频 ID') or ''}",
                    "",
                    f"- 来源剧名：{source.get('来源剧名') or ''}",
                    f"- 基准原版：`{baseline_id}`《{source.get('基准原版剧名称') or ''}》；库内语言：`{','.join(languages)}`；当前最佳排名：`{baseline_rank}`",
                    f"- 最终判定：`{decision.get('outcome') or decision.get('status') or ''}`；候选：`{normalize_id(decision.get('book_id'))}`《{decision.get('book_name') or ''}》",
                    "",
                ]
            )
            for language in languages:
                translated = translated_by_language.get(language, "")
                sections.extend([f"### 输入文本（{language}）", "", "```text", translated[:3500], "```", ""])
                windows = best_baseline_windows(
                    connection,
                    book_id=baseline_id,
                    language=language,
                    query_text=translated,
                )
                sections.extend([f"### 基准原版内部最佳窗口（{language}）", ""])
                for window in windows:
                    sections.extend(
                        [
                            f"第 `{window['episode_order']}` 集 `{window['window_uid']}`，共享 `{window['shared_count']}`，"
                            f"查询覆盖 `{window['query_coverage']:.4f}`，证据覆盖 `{window['evidence_coverage']:.4f}`。",
                            "",
                            "```text",
                            str(window["window_text"] or ""),
                            "```",
                            "",
                        ]
                    )
            sections.extend(["### 当前各目标语言 Top1", ""])
            if attempts:
                for attempt in attempts:
                    candidates = attempt.get("candidates") or []
                    top = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
                    sections.extend(
                        [
                            f"- `{attempt.get('target_language_code')}`：`{normalize_id(top.get('book_id'))}`《{top.get('book_name') or ''}》，"
                            f"语义 `{top.get('semantic_score')}`，共享 `{top.get('shared_match_count')}`。",
                            "",
                            "```text",
                            str(top.get("evidence_text") or ""),
                            "```",
                            "",
                        ]
                    )
            else:
                candidates = data.get("candidates") or []
                top = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
                evidence = top.get("evidence") if isinstance(top.get("evidence"), dict) else {}
                sections.extend(
                    [
                        f"- `{query_language}`：`{normalize_id(top.get('book_id'))}`《{top.get('book_name') or ''}》，语义 `{top.get('semantic_score')}`。",
                        "",
                        "```text",
                        str(evidence.get("window_text") or evidence.get("window_text_preview") or ""),
                        "```",
                        "",
                    ]
                )
    finally:
        connection.close()
    sections.insert(2, f"共提取 {count} 条有效跨语言样本。")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")
    print(json.dumps({"non_top1_count": count, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
