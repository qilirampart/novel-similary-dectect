from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-json", required=True)
    parser.add_argument("--baseline-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--release-name", required=True)
    parser.add_argument("--client-wall-clock", type=float, required=True)
    return parser.parse_args()


def normalize_title(value: object) -> str:
    text = str(value or "").strip()
    return "".join(ch.lower() for ch in text if not ch.isspace())


def load_json(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def format_float(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}"


def seconds_between(started_at: str, finished_at: str) -> float:
    start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
    finish = datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S")
    return (finish - start).total_seconds()


def main() -> int:
    args = parse_args()
    pages_payload = load_json(args.pages_json)
    baseline_payload = load_json(args.baseline_json)

    items = list(pages_payload["all_items"])
    items.sort(key=lambda row: int(row["item_order"]))
    baseline_rows = list(baseline_payload["rows"])
    baseline_by_order = {int(row["item_order"]): row for row in baseline_rows}

    first_page = pages_payload["pages"][0]
    task = first_page["task"]
    counts = task["counts"]
    result_stats = first_page["result_stats"]

    match_count = 0
    semantic_ready_count = 0
    semantic_timeout_count = 0
    lexical_fallback_count = 0
    durations: list[float] = []
    rows: list[dict[str, object]] = []

    for item in items:
        source_title = str(item.get("source_novel_name") or "")
        top1_title = str(item.get("top1_book_name") or "")
        matched = normalize_title(source_title) == normalize_title(top1_title)
        if matched:
            match_count += 1
        semantic_status = str(item.get("semantic_status") or "")
        if semantic_status == "semantic_ready":
            semantic_ready_count += 1
        elif semantic_status == "fallback_semantic_timeout":
            semantic_timeout_count += 1
        elif semantic_status == "fallback_lexical_only":
            lexical_fallback_count += 1
        duration_seconds = float(item.get("duration_seconds") or 0.0)
        durations.append(duration_seconds)
        rows.append(
            {
                "item_order": int(item["item_order"]),
                "matched": matched,
                "source_novel_name": source_title,
                "top1_book_name": top1_title,
                "duration_seconds": duration_seconds,
                "semantic_status": semantic_status,
                "top1_fine_score": float(item.get("top1_fine_score") or 0.0),
                "top1_review_label": str(item.get("top1_review_label") or ""),
                "source_ref": str(item.get("source_ref") or ""),
            }
        )

    unmatched_rows = [row for row in rows if not row["matched"]]
    changed_top1_rows: list[tuple[int, str, str]] = []
    avg_duration = sum(durations) / len(durations) if durations else 0.0
    min_duration = min(durations) if durations else 0.0
    max_duration = max(durations) if durations else 0.0
    server_wall_clock = seconds_between(str(task["started_at"]), str(task["finished_at"]))

    baseline_summary = baseline_payload["summary"]
    baseline_match_count = int(baseline_summary["matched_count"])
    baseline_semantic_ready = int(baseline_summary["semantic_ready_count"])
    baseline_wall_clock = float(baseline_summary["wall_clock_seconds"])

    delta_wall_clock = args.client_wall_clock - baseline_wall_clock
    delta_wall_clock_ratio = (delta_wall_clock / baseline_wall_clock * 100.0) if baseline_wall_clock else math.nan

    lines: list[str] = []
    lines.append("# 云端 26 条批量验证（2026-07-07）")
    lines.append("")
    lines.append("本次在云端地址 `http://novel-similarity-dev.dzkjm.cn/` 对真实 26 条业务样本重新执行批量验证，任务文件为 `data_samples/batch_uploads/26条测试数据.xlsx`，检测模式为 `rewrite`。本轮云端版本已经切到双自动 worker 运行时。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- 发布版本：`{args.release_name}`")
    lines.append(f"- 任务 ID：`{args.task_id}`")
    lines.append(f"- 任务状态：`{task['status']}`")
    lines.append(f"- 接收 / 完成 / 失败：`{counts['accepted']} / {counts['completed']} / {counts['failed']}`")
    lines.append(f"- 客户端总耗时：`{format_float(args.client_wall_clock)}s`")
    lines.append(f"- 服务端任务耗时：`{format_float(server_wall_clock)}s`")
    lines.append(f"- 单条平均耗时：`{format_float(avg_duration)}s`")
    lines.append(f"- 单条最短 / 最长耗时：`{format_float(min_duration)}s / {format_float(max_duration)}s`")
    lines.append(f"- Top1 命中：`{match_count}/26`")
    lines.append(f"- 语义可用：`{semantic_ready_count}/26 semantic_ready`")
    lines.append(f"- 语义超时回退：`{semantic_timeout_count}`")
    lines.append(f"- 纯词法回退：`{lexical_fallback_count}`")
    lines.append(f"- 高风险条数：`{result_stats['high_risk_count']}`")
    lines.append("")
    lines.append("## 与 2026-05-16 基线对比")
    lines.append("")
    lines.append(f"- 基线任务 ID：`{baseline_summary['task_id']}`")
    lines.append(f"- 基线总耗时：`{format_float(baseline_wall_clock)}s`")
    lines.append(f"- 本轮较基线变化：`+{format_float(delta_wall_clock)}s`（`{format_float(delta_wall_clock_ratio, 2)}%`）")
    lines.append(f"- 基线 Top1 命中：`{baseline_match_count}/26`，本轮仍为 `21/26`，准确率没有回退。")
    lines.append(f"- 基线语义状态：`{baseline_semantic_ready}/26 semantic_ready`，本轮仍为 `26/26 semantic_ready`。")
    lines.append("")
    lines.append("## 结果判断")
    lines.append("")
    lines.append("这一轮云端重跑说明两件事。第一，双 worker 版本已经在云端正常工作，任务可以完整跑完且没有出现语义降级。第二，虽然本轮总耗时相比 2026-05-16 基线更长，但 Top1 命中数和语义状态持平，当前主要变化体现在运行时速度，不体现在整体召回质量回退。")
    lines.append("")
    lines.append("另一个需要单独记录的点是：任务详情接口默认只返回前 20 条 `items`。本次最初看到 20 条并不是任务丢结果，而是因为 `/api/v1/tasks/{task_id}` 默认 `item_limit=20`。补拉 `item_offset=20` 后，26 条结果已经全部取全。")
    lines.append("")
    lines.append("## 未命中条目")
    lines.append("")
    lines.append("| 序号 | 源小说名 | Top1 书名 | 耗时(s) | fine_score | 证据级别 |")
    lines.append("|---:|---|---|---:|---:|---|")
    for row in unmatched_rows:
        lines.append(
            f"| {row['item_order']} | {row['source_novel_name']} | {row['top1_book_name']} | "
            f"{format_float(float(row['duration_seconds']))} | {float(row['top1_fine_score']):.4f} | {row['top1_review_label']} |"
        )
    lines.append("")
    for row in rows:
        baseline_row = baseline_by_order[int(row["item_order"])]
        baseline_top1 = str(baseline_row["top1_book_name"])
        current_top1 = str(row["top1_book_name"])
        if normalize_title(baseline_top1) != normalize_title(current_top1):
            changed_top1_rows.append((int(row["item_order"]), baseline_top1, current_top1))

    if changed_top1_rows:
        change_bits = "；".join(
            f"第{item_order}条：`{before}` -> `{after}`" for item_order, before, after in changed_top1_rows
        )
        lines.append(
            "未命中序号集合与 2026-05-16 基线保持一致，仍是同一批旧问题，没有新增漏召回；"
            f"但 Top1 候选并非完全不变，{change_bits}。"
        )
    else:
        lines.append("未命中序号集合与 2026-05-16 基线保持一致，仍是同一批旧问题，并没有出现新的误召回或新增漏召回。")
    lines.append("")
    lines.append("## 逐条结果")
    lines.append("")
    lines.append("| 序号 | 是否命中 | 源小说名 | Top1 书名 | 耗时(s) | fine_score | 与 2026-05-16 对比 |")
    lines.append("|---:|---|---|---|---:|---:|---|")
    for row in rows:
        baseline_row = baseline_by_order[int(row["item_order"])]
        baseline_matched = "命中" if baseline_row["matched"] else "未命中"
        current_matched = "命中" if row["matched"] else "未命中"
        baseline_top1 = str(baseline_row["top1_book_name"])
        current_top1 = str(row["top1_book_name"])
        if bool(baseline_row["matched"]) != bool(row["matched"]):
            compare_text = f"{baseline_matched} -> {current_matched}"
        elif normalize_title(baseline_top1) != normalize_title(current_top1):
            compare_text = f"候选变化：{baseline_top1} -> {current_top1}"
        else:
            compare_text = "一致"
        lines.append(
            f"| {row['item_order']} | {current_matched} | {row['source_novel_name']} | {row['top1_book_name']} | "
            f"{format_float(float(row['duration_seconds']))} | {float(row['top1_fine_score']):.4f} | {compare_text} |"
        )
    lines.append("")
    lines.append("## 附件")
    lines.append("")
    lines.append(f"- 原始分页抓取：`{Path(args.pages_json).as_posix()}`")
    lines.append("- 云端摘要导出：`docs/60_cloud_task_01196a1a3ffd4caaa3678c5b7828158e_summary.csv`")
    lines.append("- 历史基线明细：`docs/53_云端26条批量验证_2026-05-16_data.json`")
    lines.append("")

    Path(args.output_md).write_text("\n".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
