from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import openpyxl


ROOT = Path(r"E:\点众\YouTube字幕核验助手工作区")
RESULT_PATH = ROOT / "output" / "youtube_matching_results_after_160_20260827.xlsx"
TASK_PATH = ROOT / "output" / "matching_task_80c62c52.json"
BASELINE_PATH = ROOT / "output" / "分销平台6个频道的推送视频信息0827_含原版剧.xlsx"
EXISTING_TOTAL_PATH = ROOT / "output" / "结果比对文件夹" / "字幕匹配结果_基准表交叉核验总表_v3.xlsx"
DB_PATH = Path(r"E:\点众\小说库相似度比对服务工作区\data\drama_subtitle_similarity_v1.sqlite3")
OUT_PATH = ROOT / "output" / "结果比对文件夹" / "108条匹配与基准表交叉核验分析.json"


def clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def read_xlsx(path: Path, sheet_name: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    iterator = ws.iter_rows(values_only=True)
    headers = [clean(x) for x in next(iterator)]
    rows = []
    for values in iterator:
        if any(x is not None and clean(x) for x in values):
            rows.append(dict(zip(headers, values)))
    return headers, rows


def parse_json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(clean(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def normalize_id(value: Any) -> str:
    text = clean(value)
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def candidate_ids(row: dict[str, Any]) -> list[str]:
    found: list[str] = []
    selected = normalize_id(row.get("Book ID"))
    if selected:
        found.extend(re.findall(r"\b\d{8,}\b", selected))
    for field in ("强证据候选", "命中证据"):
        for item in parse_json_list(row.get(field)):
            if isinstance(item, dict):
                value = normalize_id(item.get("book_id") or item.get("Book ID"))
                if re.fullmatch(r"\d{8,}", value):
                    found.append(value)
    return list(dict.fromkeys(found))


def inventory_state(record: dict[str, Any] | None) -> str:
    if record is None:
        return "未录入字幕库"
    if record.get("first10_has_real_subtitles"):
        return "已录入，前10集有字幕"
    if record.get("has_any_real_subtitles"):
        return "已录入，有字幕但前10集无字幕"
    return "已录入，但无可用字幕"


def main() -> None:
    _, result_rows = read_xlsx(RESULT_PATH)
    _, baseline_rows = read_xlsx(BASELINE_PATH)
    _, existing_rows = read_xlsx(EXISTING_TOTAL_PATH, sheet_name="结果总表")
    task = json.loads(TASK_PATH.read_text(encoding="utf-8"))

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        inventory_rows = connection.execute(
            "SELECT book_id, book_name, has_any_real_subtitles, first10_has_real_subtitles, "
            "total_subtitle_line_count, first10_subtitle_line_count FROM drama_books"
        ).fetchall()
    finally:
        connection.close()
    inventory = {normalize_id(row["book_id"]): dict(row) for row in inventory_rows}

    baseline_by_video: dict[str, list[dict[str, Any]]] = {}
    for row in baseline_rows:
        video_id = normalize_id(row.get("youtube_video_id"))
        if video_id:
            baseline_by_video.setdefault(video_id, []).append(row)

    baseline_push_ids = {
        normalize_id(row.get("drama_id")) for row in baseline_rows if normalize_id(row.get("drama_id"))
    }
    baseline_original_ids = {
        normalize_id(row.get("原版剧ID")) for row in baseline_rows if normalize_id(row.get("原版剧ID"))
    }
    existing_video_ids = {
        normalize_id(row.get("视频 ID")) for row in existing_rows if normalize_id(row.get("视频 ID"))
    }

    comparison: list[dict[str, Any]] = []
    for index, row in enumerate(result_rows, start=2):
        video_id = normalize_id(row.get("视频 ID"))
        matches = baseline_by_video.get(video_id, [])
        baseline = matches[0] if matches else {}
        baseline_push_id = normalize_id(baseline.get("drama_id"))
        baseline_original_id = normalize_id(baseline.get("原版剧ID"))
        ids = candidate_ids(row)
        selected_candidates = re.findall(r"\b\d{8,}\b", normalize_id(row.get("Book ID")))
        selected_id = selected_candidates[0] if selected_candidates else ""
        if not selected_id and ids:
            selected_id = ids[0]
        if baseline_original_id and selected_id == baseline_original_id:
            relation = "系统候选等于基准原版 ID"
        elif baseline_push_id and selected_id == baseline_push_id:
            relation = "系统候选等于基准推送剧 ID"
        elif selected_id and selected_id in {baseline_push_id, baseline_original_id}:
            relation = "系统候选等于基准 ID"
        elif selected_id:
            relation = "系统候选为其他库内 ID"
        else:
            relation = "未解析出系统候选 ID"

        evidence = parse_json_list(row.get("强证据候选"))
        evidence_ids = []
        for item in evidence:
            if isinstance(item, dict):
                value = normalize_id(item.get("book_id") or item.get("Book ID"))
                if value:
                    evidence_ids.append(value)
        if not evidence_ids:
            evidence_ids = ids
        top10_original = baseline_original_id in set(evidence_ids) if baseline_original_id else False
        selected_record = inventory.get(selected_id)
        selected_global_relation = "不在基准表 ID 集合"
        if selected_id and selected_id in baseline_original_ids:
            selected_global_relation = "系统返回 ID 属于基准原版 ID 集合"
        elif selected_id and selected_id in baseline_push_ids:
            selected_global_relation = "系统返回 ID 属于基准推送剧 ID 集合"
        evidence_global_relation = "候选证据未发现基准表 ID"
        if set(evidence_ids) & baseline_original_ids:
            evidence_global_relation = "候选证据含基准原版 ID"
        elif set(evidence_ids) & baseline_push_ids:
            evidence_global_relation = "候选证据含基准推送剧 ID"
        comparison.append(
            {
                "结果源行号": index,
                "视频 ID": video_id,
                "来源频道": clean(row.get("来源频道")),
                "来源剧名": clean(row.get("来源剧名")),
                "基准关联行数": len(matches),
                "基准推送剧 ID": baseline_push_id,
                "基准推送剧名": clean(baseline.get("drama_name")),
                "基准原版剧 ID": baseline_original_id,
                "基准原版剧名": clean(baseline.get("原版剧名称")),
                "基准 post_type": clean(baseline.get("post_type")),
                "系统匹配状态": clean(row.get("匹配状态")),
                "用户结论": clean(row.get("用户结论")),
                "系统返回剧名": clean(row.get("命中剧名")),
                "系统返回 Book ID": selected_id,
                "系统候选 ID 集合": " | ".join(ids),
                "候选证据 ID 集合": " | ".join(evidence_ids),
                "基准原版是否进入候选证据": "是" if top10_original else "否",
                "系统候选库状态": inventory_state(selected_record),
                "系统候选库内书名": clean(selected_record.get("book_name")) if selected_record else "",
                "与基准关系": relation,
                "与基准全表 ID 关系": selected_global_relation,
                "候选证据与基准全表 ID 关系": evidence_global_relation,
                "与历史结果表关系": "历史结果表已有同视频 ID" if video_id in existing_video_ids else "本批次新增视频 ID",
                "命中时间范围": clean(row.get("命中时间范围")),
                "匹配原因": clean(row.get("匹配原因")),
                "确认命中数": clean(row.get("确认命中数")),
                "待复核数": clean(row.get("待复核数")),
                "未命中数": clean(row.get("未命中数")),
                "翻译回退": clean(row.get("翻译回退")),
                "服务端执行": clean(row.get("服务端执行")),
                "强证据候选": clean(row.get("强证据候选")),
                "命中证据": clean(row.get("命中证据")),
                "完整字幕": clean(row.get("完整字幕")),
            }
        )

    result_video_ids = {row["视频 ID"] for row in comparison if row["视频 ID"]}
    baseline_valid_ids = {
        normalize_id(row.get("youtube_video_id"))
        for row in baseline_rows
        if normalize_id(row.get("youtube_video_id")) and not normalize_id(row.get("youtube_video_id")).startswith("#")
    }

    status_counts = Counter(row["系统匹配状态"] for row in comparison)
    joined = [row for row in comparison if row["基准关联行数"]]
    joined_relation_counts = Counter(row["与基准关系"] for row in joined)
    original_in_candidate = sum(row["基准原版是否进入候选证据"] == "是" for row in joined)
    original_selected = sum(row["与基准关系"] == "系统候选等于基准原版 ID" for row in joined)
    no_match_original_in_library = sum(
        row["系统匹配状态"] == "no_match" and row["基准原版剧 ID"] in inventory for row in joined if row["基准原版剧 ID"]
    )
    candidate_ids_all = {value for row in comparison for value in row["候选证据 ID 集合"].split(" | ") if value}
    selected_ids_all = {row["系统返回 Book ID"] for row in comparison if row["系统返回 Book ID"]}
    baseline_candidate_overlap = candidate_ids_all & (baseline_push_ids | baseline_original_ids)
    baseline_selected_overlap = selected_ids_all & (baseline_push_ids | baseline_original_ids)

    summary = {
        "generated_at": "2026-08-28",
        "result_file": str(RESULT_PATH),
        "baseline_file": str(BASELINE_PATH),
        "task_file": str(TASK_PATH),
        "task": task.get("task", {}),
        "baseline": {
            "raw_rows": len(baseline_rows),
            "valid_unique_video_ids": len(baseline_valid_ids),
            "duplicate_video_ids": len(baseline_rows) - len(baseline_valid_ids),
        },
        "results": {
            "rows": len(comparison),
            "unique_video_ids": len(result_video_ids),
            "status_counts": dict(status_counts),
            "existing_result_rows": len(existing_rows),
            "overlap_with_existing_result_video_ids": len(result_video_ids & existing_video_ids),
            "combined_unique_video_ids_with_existing_results": len(result_video_ids | existing_video_ids),
        },
        "join": {
            "result_baseline_intersection": len(joined),
            "result_only": len(comparison) - len(joined),
            "baseline_only_against_108": len(baseline_valid_ids - result_video_ids),
            "relation_counts_joined": dict(joined_relation_counts),
            "original_id_in_candidate_evidence": original_in_candidate,
            "original_id_selected_as_system_book_id": original_selected,
            "no_match_with_original_id_in_subtitle_db": no_match_original_in_library,
            "baseline_push_id_count": len(baseline_push_ids),
            "baseline_original_id_count": len(baseline_original_ids),
            "unique_candidate_evidence_id_count": len(candidate_ids_all),
            "candidate_evidence_ids_overlapping_baseline_ids": sorted(baseline_candidate_overlap),
            "selected_book_ids_overlapping_baseline_ids": sorted(baseline_selected_overlap),
        },
        "rows": comparison,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("baseline", "results", "join")}, ensure_ascii=True, indent=2))
    print(OUT_PATH)


if __name__ == "__main__":
    main()
