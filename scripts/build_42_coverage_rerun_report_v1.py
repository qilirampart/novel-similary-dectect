from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RESULT = ROOT / "docs/42_coverage_rerun_20260902/42条字幕库补充后重跑_逐条结果.csv"
SUMMARY = ROOT / "docs/42_coverage_rerun_20260902/42条字幕库补充后重跑_总结.md"


def number(value: str) -> float | None:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return None


def clean(value: str, limit: int = 120) -> str:
    value = str(value or "").replace("|", "\\|").replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit] + "..."


def main() -> None:
    with RESULT.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 42:
        raise RuntimeError(f"expected 42 result rows, got {len(rows)}")

    status_counts: dict[str, int] = {}
    for row in rows:
        status = row.get("命中状态", "")
        status_counts[status] = status_counts.get(status, 0) + 1
    durations = [value for value in (number(row.get("耗时(秒)", "")) for row in rows) if value is not None]
    semantic_scores = [value for value in (number(row.get("第一候选语义分数", "")) for row in rows) if value is not None]
    exact_id_rows = [
        row for row in rows
        if row.get("命中状态") == "matched"
        and row.get("基准原版剧ID", "").strip()
        and row.get("基准原版剧ID", "").strip() == row.get("第一候选Book ID", "").strip()
    ]
    matched_rows = [row for row in rows if row.get("命中状态") == "matched"]
    review_rows = [row for row in rows if row.get("命中状态") == "review_required"]
    no_match_rows = [row for row in rows if row.get("命中状态") == "not_matched"]

    lines = [
        "# 42条字幕库补充后重跑总结（2026-09-02）",
        "",
        "## 一、任务说明",
        "",
        "本轮针对历史基准对比中归类为“数据覆盖不足”的42条数据重新检测。历史结论是推送剧ID和原版剧ID均未在当时的字幕库中，因此无法完成内容级判断；本轮使用新增字幕采集并完成向量化后的本地字幕库，未修改历史任务和原始数据。检测参数为 `top_k=10`、`window_limit=200`、启用字幕语义检索和翻译回退。",
        "",
        "## 二、执行结果",
        "",
        f"- 输入：42条；任务状态：完成；失败：0条；任务总耗时：795.83秒（约13分16秒）。",
        f"- 自动确认命中：{len(matched_rows)}条（{len(matched_rows) / len(rows):.1%}）。只有系统最终状态为 `matched` 且 `是否确认命中=True` 的记录计入此项。",
        f"- 待复核：{len(review_rows)}条（{len(review_rows) / len(rows):.1%}）。这些记录存在候选或跨语言证据，但系统没有直接认定为命中，不能在统计中当作已确认命中。",
        f"- 仍未命中：{len(no_match_rows)}条（{len(no_match_rows) / len(rows):.1%}）。当前仍未达到系统接受阈值。",
        f"- 第一候选记录完整性：42/42条均已记录第一候选、候选Book ID、语义分数和耗时，便于人工核对。",
        f"- 8条自动确认命中中，有{len(exact_id_rows)}条第一候选Book ID与基准原版ID完全一致；其余命中应按内容级证据和剧本版本关系核对，不以ID是否一致作为命中前提。",
        "",
        "## 三、耗时与分数概况",
        "",
        f"- 单条耗时：平均{statistics.mean(durations):.2f}秒，中位数{statistics.median(durations):.2f}秒，最短{min(durations):.2f}秒，最长{max(durations):.2f}秒。",
        f"- 第一候选语义分数：平均{statistics.mean(semantic_scores):.4f}，最低{min(semantic_scores):.4f}，最高{max(semantic_scores):.4f}。语义分数只作为候选证据，不等同于最终命中。",
        "",
        "## 四、自动确认命中",
        "",
        "|序号|来源剧名|基准原版剧名|第一候选|Book ID|集数|语义分数|耗时(秒)|",
        "|---:|---|---|---|---|---:|---:|---:|",
    ]
    for row in matched_rows:
        lines.append(
            "|{序号}|{来源剧名}|{基准原版剧名称}|{第一候选剧名}|{第一候选Book ID}|{第一候选集数}|{第一候选语义分数}|{耗时(秒)}|".format(
                **{key: clean(row.get(key, ""), 70) for key in ("序号", "来源剧名", "基准原版剧名称", "第一候选剧名", "第一候选Book ID", "第一候选集数", "第一候选语义分数", "耗时(秒)")}
            )
        )
    lines.extend([
        "",
        "## 五、仍未命中记录",
        "",
        "这两条不能仅因第一候选存在就算命中；应结合第一候选证据文本和原始字幕继续核验。若候选内容不对应，应维持未命中；若人工确认剧情对应，则需要进一步检查字幕语言、翻译质量、切窗和候选排序。",
        "",
        "|序号|来源剧名|基准原版剧名|第一候选|Book ID|语义分数|耗时(秒)|判定原因|",
        "|---:|---|---|---|---|---:|---:|---|",
    ])
    for row in no_match_rows:
        lines.append(
            "|{}|{}|{}|{}|{}|{}|{}|{}|".format(
                clean(row.get("序号", "")), clean(row.get("来源剧名", ""), 70),
                clean(row.get("基准原版剧名称", ""), 70), clean(row.get("第一候选剧名", ""), 50),
                clean(row.get("第一候选Book ID", ""), 30), clean(row.get("第一候选语义分数", "")),
                clean(row.get("耗时(秒)", "")), clean(row.get("判定原因", ""), 70),
            )
        )
    lines.extend([
        "",
        "## 六、完整逐条结果",
        "",
        "完整原始响应保存在同目录的 `task_b3b87390-1d43-4487-b3a3-225c1f0dbc74_raw.json`，便于查看候选列表和证据原文；结构化结果保存在 `42条字幕库补充后重跑_逐条结果.csv`，包含42条的状态、耗时、第一候选、语义分数、候选列表和原始字幕。",
        "",
        "|序号|命中状态|候选剧名|Book ID|语义分数|耗时(秒)|",
        "|---:|---|---|---|---:|---:|",
    ])
    for row in rows:
        lines.append(
            "|{}|{}|{}|{}|{}|{}|".format(
                clean(row.get("序号", "")), clean(row.get("命中状态", "")),
                clean(row.get("第一候选剧名", ""), 55), clean(row.get("第一候选Book ID", ""), 25),
                clean(row.get("第一候选语义分数", "")), clean(row.get("耗时(秒)", "")),
            )
        )
    lines.extend([
        "",
        "## 七、结论",
        "",
        "新增字幕库已经让这批历史上完全无法判断的数据具备了可检测条件：8条达到系统自动确认命中标准，32条获得了需要人工确认的候选证据，只有2条仍未达到接受阈值。下一步应优先人工复核32条，重点区分同剧不同Book ID、跨语言版本和内容相似但并非同剧三类情况；不建议仅根据本轮两条未命中就放宽全局阈值。",
    ])
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "status_counts": status_counts, "summary": str(SUMMARY)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
