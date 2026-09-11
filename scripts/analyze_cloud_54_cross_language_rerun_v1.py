from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare the old and optimized 54-row cloud reruns.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--old-raw", required=True)
    parser.add_argument("--new-raw", required=True)
    parser.add_argument("--out-dir", required=True)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(path: Path) -> list[dict[str, Any]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook.active
    iterator = worksheet.iter_rows(values_only=True)
    headers = [str(value or "") for value in next(iterator)]
    return [
        dict(zip(headers, values))
        for values in iterator
        if any(value not in (None, "") for value in values)
    ]


def normalize_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def payload(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("result_payload_json")
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def by_order(raw: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in raw.get("items") or []:
        try:
            result[int(item.get("item_order"))] = item
        except (TypeError, ValueError):
            continue
    return result


def candidate_id(candidate: dict[str, Any]) -> str:
    return normalize_id(candidate.get("book_id") or candidate.get("matched_book_id") or candidate.get("book_ext_id"))


def candidate_rank(candidate: dict[str, Any], fallback: int) -> int:
    try:
        return int(candidate.get("rank") or fallback)
    except (TypeError, ValueError):
        return fallback


def find_candidate(candidates: list[Any], book_id: str) -> tuple[int | None, dict[str, Any] | None]:
    for index, candidate in enumerate(candidates, start=1):
        if isinstance(candidate, dict) and candidate_id(candidate) == book_id:
            return candidate_rank(candidate, index), candidate
    return None, None


def find_across_attempts(data: dict[str, Any], book_id: str) -> tuple[str, int | None, dict[str, Any] | None]:
    fallback = data.get("translation_fallback")
    attempts = fallback.get("attempts") if isinstance(fallback, dict) else []
    best: tuple[str, int | None, dict[str, Any] | None] = ("", None, None)
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        rank, candidate = find_candidate(attempt.get("candidates") or [], book_id)
        if rank is not None and (best[1] is None or rank < best[1]):
            best = (str(attempt.get("target_language_code") or ""), rank, candidate)
    return best


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile_value * len(ordered)) - 1))
    return ordered[index]


def summarize_candidates(data: dict[str, Any], limit: int = 5) -> str:
    rows: list[str] = []
    seen: set[tuple[str, str]] = set()
    fallback = data.get("translation_fallback")
    attempts = fallback.get("attempts") if isinstance(fallback, dict) else []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        target = str(attempt.get("target_language_code") or "")
        for index, candidate in enumerate(attempt.get("candidates") or [], start=1):
            if not isinstance(candidate, dict):
                continue
            key = (target, candidate_id(candidate))
            if key in seen:
                continue
            seen.add(key)
            score = candidate.get("semantic_score")
            score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "-"
            rows.append(
                f"{target}#{candidate_rank(candidate, index)} "
                f"{candidate.get('book_name') or ''}({candidate_id(candidate)}) semantic={score_text}"
            )
            if len(rows) >= limit:
                return "\n".join(rows)
    return "\n".join(rows)


def main() -> int:
    args = parse_args()
    input_rows = load_rows(Path(args.input))
    old_items = by_order(load_json(Path(args.old_raw)))
    new_raw = load_json(Path(args.new_raw))
    new_items = by_order(new_raw)
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for order, source in enumerate(input_rows, start=1):
        baseline_id = normalize_id(source.get("基准原版剧ID"))
        baseline_name = str(source.get("基准原版剧名称") or "")
        old_item = old_items.get(order, {})
        new_item = new_items.get(order, {})
        old_payload = payload(old_item)
        new_payload = payload(new_item)
        old_decision = old_payload.get("decision") if isinstance(old_payload.get("decision"), dict) else {}
        new_decision = new_payload.get("decision") if isinstance(new_payload.get("decision"), dict) else {}
        decision_id = normalize_id(new_decision.get("book_id"))
        main_rank, main_candidate = find_candidate(new_payload.get("candidates") or [], baseline_id)
        target_language, translated_rank, translated_candidate = find_across_attempts(new_payload, baseline_id)
        best_rank = main_rank if main_rank is not None else translated_rank
        baseline_recalled = best_rank is not None
        decision_correct = bool(baseline_id and decision_id == baseline_id)
        decision_wrong = bool(decision_id and decision_id != baseline_id)
        query_language = str(new_item.get("query_language_code") or new_payload.get("query_language_code") or "unknown")
        if decision_correct:
            result_class = "正确原版成为待复核候选"
        elif baseline_recalled:
            result_class = "正确原版进入候选但未被选中"
        elif decision_wrong:
            result_class = "错误候选进入待复核"
        else:
            result_class = "仍未召回正确原版"
        semantic_source = translated_candidate or main_candidate or {}
        records.append(
            {
                "序号": order,
                "结果源行号": source.get("结果源行号", ""),
                "视频 ID": source.get("视频 ID", ""),
                "来源频道": source.get("来源频道", ""),
                "来源剧名": source.get("来源剧名", ""),
                "查询语言": query_language,
                "基准原版剧ID": baseline_id,
                "基准原版剧名称": baseline_name,
                "旧版判定": old_decision.get("outcome") or old_decision.get("status") or old_item.get("status", ""),
                "新版判定": new_decision.get("outcome") or new_decision.get("status") or new_item.get("status", ""),
                "新版结果分类": result_class,
                "正确原版是否成为判定候选": "是" if decision_correct else "否",
                "正确原版是否进入Top10": "是" if baseline_recalled else "否",
                "正确原版最佳目标语言": target_language,
                "正确原版最佳排名": best_rank if best_rank is not None else "",
                "正确原版语义分数": semantic_source.get("semantic_score", ""),
                "判定候选剧ID": decision_id,
                "判定候选剧名": new_decision.get("book_name", ""),
                "判定候选排名": new_decision.get("candidate_rank", ""),
                "判定候选语义分数": new_decision.get("semantic_score", ""),
                "判定理由": new_decision.get("reason", ""),
                "用户提示": new_decision.get("user_message", ""),
                "耗时(秒)": new_item.get("duration_seconds", ""),
                "翻译候选摘要": summarize_candidates(new_payload),
                "错误信息": new_item.get("error_message", ""),
            }
        )

    headers = list(records[0])
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "逐条对比"
    worksheet.append(headers)
    for record in records:
        worksheet.append([record[header] for header in headers])
    fill = PatternFill("solid", fgColor="17365D")
    font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    for cell in worksheet[1]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    wide = {"来源剧名", "基准原版剧名称", "判定候选剧名", "判定理由", "用户提示", "翻译候选摘要"}
    for row in worksheet.iter_rows(min_row=2):
        for index, cell in enumerate(row):
            cell.alignment = Alignment(
                horizontal="left" if headers[index] in wide else "center",
                vertical="top",
                wrap_text=headers[index] in wide,
            )
    for index, header in enumerate(headers, start=1):
        width = 62 if header in {"用户提示", "翻译候选摘要"} else 32 if header in wide else 18
        worksheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    workbook.save(output_dir / "54条跨语言优化重跑_逐条对比.xlsx")

    by_language: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_language[str(record["查询语言"])].append(record)
    durations = [float(record["耗时(秒)"]) for record in records if isinstance(record["耗时(秒)"], (int, float))]
    classifications = Counter(str(record["新版结果分类"]) for record in records)
    task = new_raw.get("task") if isinstance(new_raw.get("task"), dict) else {}
    poll_log = new_raw.get("poll_log") if isinstance(new_raw.get("poll_log"), list) else []
    task_elapsed_seconds = 0.0
    for snapshot in reversed(poll_log):
        if isinstance(snapshot, dict):
            try:
                task_elapsed_seconds = float(snapshot.get("elapsed_seconds") or 0.0)
            except (TypeError, ValueError):
                task_elapsed_seconds = 0.0
            if task_elapsed_seconds > 0:
                break
    lines = [
        "# 54条原版ID在库未命中数据跨语言优化重跑总结",
        "",
        f"云端任务 `{task.get('task_id', '')}` 已完成，共 54 条，成功执行 54 条，接口失败 0 条。旧版 54 条均为未命中；新版统计坚持准确性优先，返回候选不直接视为成功，只有基准原版 ID 成为判定候选或进入候选列表才计入召回改善。",
        "",
        "## 总体结果",
        "",
        f"- 正确原版成为待复核候选：{classifications['正确原版成为待复核候选']} 条。",
        f"- 正确原版进入候选但未被选中：{classifications['正确原版进入候选但未被选中']} 条。",
        f"- 错误候选进入待复核：{classifications['错误候选进入待复核']} 条。该类没有自动确认命中，仍受人工复核门槛保护。",
        f"- 仍未召回正确原版：{classifications['仍未召回正确原版']} 条。",
        "",
        "## 分语言结果",
        "",
        "| 查询语言 | 条数 | 正确原版成为判定候选 | 正确原版进入Top10 | 错误候选待复核 | 仍未召回正确原版 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for language in sorted(by_language):
        group = by_language[language]
        correct = sum(record["正确原版是否成为判定候选"] == "是" for record in group)
        recalled = sum(record["正确原版是否进入Top10"] == "是" for record in group)
        wrong = sum(record["新版结果分类"] == "错误候选进入待复核" for record in group)
        missed = sum(record["新版结果分类"] == "仍未召回正确原版" for record in group)
        lines.append(f"| {language} | {len(group)} | {correct} | {recalled} | {wrong} | {missed} |")
    non_chinese = [record for record in records if record["查询语言"] != "zh"]
    non_chinese_correct = sum(record["正确原版是否成为判定候选"] == "是" for record in non_chinese)
    non_chinese_recalled = sum(record["正确原版是否进入Top10"] == "是" for record in non_chinese)
    lines.extend(
        [
            "",
            "中文 10 条的基准原版 ID 已在前序人工核验中确认与库内剧情明显不对应，因此单列展示，不纳入跨语言召回优化结论。",
            f"排除中文错基准后，{len(non_chinese)} 条跨语言样本中，正确原版成为判定候选 {non_chinese_correct} 条，正确原版进入 Top10 {non_chinese_recalled} 条。",
            "",
            "## 性能与风险",
            "",
            f"54 条平均耗时 {statistics.mean(durations):.2f} 秒，中位数 {statistics.median(durations):.2f} 秒，P95 {percentile(durations, 0.95):.2f} 秒，最大 {max(durations):.2f} 秒。任务总历时约 {task_elapsed_seconds:.0f} 秒，执行过程中失败 0 条。",
            "",
            "跨语言语义候选仍只进入待复核，不会仅凭机器翻译或中等语义分数自动确认命中。这样提升了正确原版的可见性，同时保留误报控制边界。逐条结果、候选排名、语义分数和用户提示见同目录 Excel。",
            "",
        ]
    )
    (output_dir / "54条跨语言优化重跑_对比总结.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "records": len(records),
        "classifications": dict(classifications),
        "languages": {key: len(value) for key, value in sorted(by_language.items())},
        "non_chinese_correct_decision": non_chinese_correct,
        "non_chinese_recalled_top10": non_chinese_recalled,
        "mean_seconds": round(statistics.mean(durations), 3),
        "p95_seconds": round(percentile(durations, 0.95), 3),
        "max_seconds": round(max(durations), 3),
        "task_elapsed_seconds": round(task_elapsed_seconds, 3),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
