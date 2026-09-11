from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(r"E:\点众\YouTube字幕核验助手工作区")
ANALYSIS_PATH = ROOT / "output" / "结果比对文件夹" / "108条匹配与基准表交叉核验分析.json"
REPORT_PATH = ROOT / "output" / "结果比对文件夹" / "108条匹配与基准表交叉核验汇总_2026-08-28.md"
CSV_PATH = ROOT / "output" / "结果比对文件夹" / "108条匹配与基准表逐条对比_2026-08-28.csv"


def pct(count: int, total: int) -> str:
    return f"{count / total:.1%}" if total else "0.0%"


def md_cell(value: object, limit: int = 120) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def main() -> None:
    data = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    rows = data["rows"]
    total = len(rows)
    task = data["task"]
    status_counts = Counter(row["系统匹配状态"] for row in rows)
    selected_relation = Counter(row["与基准全表 ID 关系"] for row in rows)
    evidence_relation = Counter(row["候选证据与基准全表 ID 关系"] for row in rows)
    joined = data["join"]

    starts = datetime.fromisoformat(task["started_at"])
    finishes = datetime.fromisoformat(task["finished_at"])
    elapsed = (finishes - starts).total_seconds()

    baseline_id_names: dict[str, dict[str, str]] = {}
    # The detailed row data contains the matched book name for selected candidates.
    for row in rows:
        selected_id = row["系统返回 Book ID"]
        selected_name = row["系统候选库内书名"]
        if selected_id and selected_id not in baseline_id_names:
            baseline_id_names[selected_id] = {
                "name": selected_name,
                "video": row["视频 ID"],
                "status": row["系统匹配状态"],
            }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.extend(
        [
            "# 108条匹配结果与基准表交叉核验汇总",
            "",
            "> 本报告更新于 2026-08-28。新增批次只做数据核验与总结，不调整召回、排序或最终判定逻辑；剧情候选 Top1 优化列入后置任务。",
            "",
            "## 一、结论摘要",
            "",
            f"昨天完成的 108 条任务已全部完成，成功 {task.get('completed_input_count', total)} 条，失败 {task.get('failed_input_count', 0)} 条。与当前基准表按 YouTube 视频 ID 严格关联后，交集为 **0 条**，因此本批次不能直接使用基准表的原版剧 ID 计算逐条召回准确率。",
            "",
            f"进一步将系统返回候选与基准表全量 ID 集合比较：108 条中有 {len(joined['selected_book_ids_overlapping_baseline_ids'])} 条首选 Book ID 出现在基准表 ID 集合中，其中应理解为跨批次 ID 交叉信号，而不是同视频的金标准命中；另有 1 个基准 ID 仅出现在候选证据中但未成为首选。其余结果没有在基准表 ID 集合中找到对应项。",
            "",
            "本轮最重要的核验结论是：108 条结果与基准表不是同一批视频，当前基准表只能用于确认少量跨批次 ID 重合，不能用于判断这 108 条每一条的原版归属。后续若要评估 Top1 召回，应补充与这 108 条视频一一对应的原版剧 ID 或人工金标。",
            "",
            "## 二、任务与结果概况",
            "",
            "| 指标 | 数量/数值 | 说明 |",
            "|---|---:|---|",
            f"| 新增匹配结果 | {total} | 结果表 108 条，视频 ID 唯一 {data['results']['unique_video_ids']} 条 |",
            f"| 既有 v3 结果记录 | {data['results']['existing_result_rows']} | 现有交叉核验总表中的历史结果记录 |",
            f"| 与既有结果同视频 ID | {data['results']['overlap_with_existing_result_video_ids']} | 新批次与历史结果表存在重复视频 ID |",
            f"| 合并后唯一视频 ID | {data['results']['combined_unique_video_ids_with_existing_results']} | 既有结果与本批次按视频 ID 去重后的数量 |",
            f"| 基准表原始记录 | {data['baseline']['raw_rows']} | 基准表总行数 |",
            f"| 基准表有效唯一视频 ID | {data['baseline']['valid_unique_video_ids']} | 排除空值与错误值后的唯一 ID 数 |",
            f"| 同视频 ID 交集 | {joined['result_baseline_intersection']} | 严格按视频 ID 关联 |",
            f"| 108 条结果独有 | {joined['result_only']} | 新结果存在、基准表无同视频 ID |",
            f"| 基准表相对 108 条独有 | {joined['baseline_only_against_108']} | 基准表有效视频未出现在本批次 |",
            f"| 任务墙钟耗时 | {elapsed:.0f} 秒 | {task['started_at']} 至 {task['finished_at']} |",
            f"| 平均墙钟耗时 | {elapsed / total:.2f} 秒/条 | 2 个 worker 的批次墙钟平均值，不等同单条串行耗时 |",
            "",
            "## 三、匹配状态分布",
            "",
            "| 系统状态 | 数量 | 占 108 条 | 口径 |",
            "|---|---:|---:|---|",
            f"| confirmed_match | {status_counts['confirmed_match']} | {pct(status_counts['confirmed_match'], total)} | 系统给出确认命中 |",
            f"| translation_assisted_match | {status_counts['translation_assisted_match']} | {pct(status_counts['translation_assisted_match'], total)} | 翻译辅助后形成候选，仍需结合证据核验 |",
            f"| content_matched_ambiguous | {status_counts['content_matched_ambiguous']} | {pct(status_counts['content_matched_ambiguous'], total)} | 内容有重合但存在多个或不确定候选 |",
            f"| potential_match | {status_counts['potential_match']} | {pct(status_counts['potential_match'], total)} | 有候选信号但证据不足以自动确认 |",
            f"| no_match | {status_counts['no_match']} | {pct(status_counts['no_match'], total)} | 未形成可接受的最终命中 |",
            "",
            "## 四、与基准表 ID 集合的交叉结果",
            "",
            "这里的比较不是同视频关联，而是将系统返回的 Book ID 与基准表中所有推送剧 ID、原版剧 ID 做集合比较。它只能说明某个 ID 在两批数据中都出现过，不能单独证明当前视频就是该基准表记录对应的剧。",
            "",
            "| 交叉口径 | 数量 | 说明 |",
            "|---|---:|---|",
            f"| 首选 ID 属于基准原版 ID 集合 | {selected_relation['系统返回 ID 属于基准原版 ID 集合']} | 首选 Book ID 在基准原版 ID 集合中 |",
            f"| 首选 ID 属于基准推送剧 ID 集合 | {selected_relation['系统返回 ID 属于基准推送剧 ID 集合']} | 首选 Book ID 在基准推送剧 ID 集合中 |",
            f"| 首选 ID 不在基准表 ID 集合 | {selected_relation['不在基准表 ID 集合']} | 不能由基准表完成 ID 对照 |",
            f"| 候选证据含基准原版 ID | {evidence_relation['候选证据含基准原版 ID']} | 基准原版 ID 出现在候选证据集合中 |",
            f"| 候选证据含基准推送剧 ID | {evidence_relation['候选证据含基准推送剧 ID']} | 基准推送剧 ID 出现在候选证据集合中 |",
            f"| 候选证据未发现基准表 ID | {evidence_relation['候选证据未发现基准表 ID']} | 候选证据未与基准 ID 集合交叉 |",
            "",
            "基准表中实际与候选证据交叉到的 6 个 ID：`" + "`、`".join(joined["candidate_evidence_ids_overlapping_baseline_ids"]) + "`。其中 5 个成为系统首选 Book ID，1 个仅出现在候选证据中。由于没有同视频基准行，当前不把这 6 条计为准确命中。",
            "",
            "## 五、逐条核验表",
            "",
            "逐条表保留 108 条结果的核心字段。‘与基准全表 ID 关系’仅代表 ID 集合重合，不代表视频级归属；‘基准同视频关联’本批次全部为否。‘与历史结果表关系’用于识别新增批次与既有 v3 结果中的重复视频。完整字幕、翻译信息、强证据候选和命中证据保存在同目录 JSON 审计文件中。",
            "",
            "| 序号 | 视频 ID | 来源剧名 | 系统状态 | 系统返回剧名 | 首选 Book ID | 候选库内书名 | 与基准全表 ID 关系 | 候选证据与基准关系 | 与历史结果表关系 |",
            "|---:|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for index, row in enumerate(rows, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    md_cell(row["视频 ID"], 24),
                    md_cell(row["来源剧名"], 42),
                    md_cell(row["系统匹配状态"], 28),
                    md_cell(row["系统返回剧名"], 36),
                    md_cell(row["系统返回 Book ID"], 24),
                    md_cell(row["系统候选库内书名"], 30),
                    md_cell(row["与基准全表 ID 关系"], 24),
                    md_cell(row["候选证据与基准全表 ID 关系"], 24),
                    md_cell(row["与历史结果表关系"], 24),
                ]
            )
            + "|"
        )
    lines.extend(
        [
            "",
            "## 六、对后续工作的影响",
            "",
            "1. 本批次暂不据此调整 Top1 排序。当前首先缺少同视频原版 ID 金标，贸然按这 108 条结果修改排序，无法区分召回问题、基准缺失问题和输入数据本身的跨版本差异。",
            "2. 后续评估应建立视频级评估表，至少包含视频 ID、原版剧 ID、推送剧 ID、输入语言、人工确认结果和证据范围；只有这些字段齐全，才能计算 Top1、Top10、误命中和未命中。",
            "3. 当前 108 条任务本身执行稳定，108/108 完成、0 失败；本轮主要暴露的是评估数据对齐问题，而不是任务执行失败。",
            "4. 剧情信息候选尽量进入 Top1 的排序优化继续后置，待补齐同批次金标后再做小步实验并回归已有稳定样本。",
            "",
            "## 七、审计文件",
            "",
            f"- 原始 108 条结果：`{data['result_file']}`",
            f"- 原始任务记录：`{data['task_file']}`",
            f"- 基准表：`{data['baseline_file']}`",
            f"- 逐条审计 JSON：`{ANALYSIS_PATH}`",
            f"- 逐条对比 CSV：`{CSV_PATH}`",
            "",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    csv_headers = [
        "序号",
        "视频 ID",
        "来源频道",
        "来源剧名",
        "基准同视频关联行数",
        "系统匹配状态",
        "用户结论",
        "系统返回剧名",
        "系统返回 Book ID",
        "系统候选库内书名",
        "系统候选库状态",
        "系统候选 ID 集合",
        "候选证据 ID 集合",
        "基准原版是否进入候选证据",
        "与基准全表 ID 关系",
        "候选证据与基准全表 ID 关系",
        "与历史结果表关系",
        "命中时间范围",
        "确认命中数",
        "待复核数",
        "未命中数",
        "翻译回退",
        "服务端执行",
    ]
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_headers, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow({"序号": index, **row})

    print(REPORT_PATH)
    print(CSV_PATH)


if __name__ == "__main__":
    main()
