from __future__ import annotations

import csv
import json
from pathlib import Path

import openpyxl


ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path("E:/点众/YouTube字幕核验助手工作区/output/结果比对文件夹/733条六频道基准对比交付汇总_2026-08-28.xlsx")
SOURCE_FILES = {
    "AI结果-日语、葡语.xlsx": Path(
        "E:/点众/YouTube字幕核验助手工作区/output/结果比对文件夹/AI结果-日语、葡语.xlsx"
    ),
    "韩国频道163条.xlsx": Path(
        "D:/Dianzhong/YouTube字幕核验助手/output/output/output/韩国频道163条.xlsx"
    ),
    "云上剧场.xlsx": Path(
        "E:/点众/YouTube字幕核验助手工作区/output/结果比对文件夹/云上剧场.xlsx"
    ),
}
OUTPUT = ROOT / "data_samples/batch_uploads/42条字幕库补充后重跑输入_20260902.csv"


def load_rows(
    path: Path,
    *,
    header_row: int = 1,
    sheet_name: str | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook[sheet_name] if sheet_name else workbook.active
    values = worksheet.iter_rows(values_only=True)
    for _ in range(header_row - 1):
        next(values)
    headers = [str(value or "") for value in next(values)]
    rows = [dict(zip(headers, row)) for row in values if row and row[0] is not None]
    workbook.close()
    return headers, rows


def main() -> None:
    _, baseline_rows = load_rows(BASELINE, header_row=4, sheet_name="97条未命中拆解")
    coverage_rows = [
        row
        for row in baseline_rows
        if str(row.get("交付归类") or "").startswith("数据覆盖不足")
    ]
    if len(coverage_rows) != 42:
        raise RuntimeError(f"expected 42 coverage rows, got {len(coverage_rows)}")

    source_by_id: dict[str, tuple[str, dict[str, object]]] = {}
    for source_name, source_path in SOURCE_FILES.items():
        _, source_rows = load_rows(source_path)
        for row in source_rows:
            video_id = str(row.get("视频 ID") or "").strip()
            if video_id:
                source_by_id[video_id] = (source_name, row)

    missing = [str(row.get("视频 ID") or "") for row in coverage_rows if str(row.get("视频 ID") or "") not in source_by_id]
    if missing:
        raise RuntimeError(f"missing source subtitles for {len(missing)} rows: {missing}")

    headers = [
        "source_ref",
        "source_video_id",
        "source_channel",
        "short_drama",
        "query_text",
        "baseline_push_id",
        "baseline_original_id",
        "baseline_original_name",
        "source_row_in_733",
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for index, baseline in enumerate(coverage_rows, start=5):
            video_id = str(baseline.get("视频 ID") or "")
            _, source = source_by_id[video_id]
            writer.writerow(
                {
                    "source_ref": video_id,
                    "source_video_id": video_id,
                    "source_channel": source.get("来源频道") or "",
                    "short_drama": source.get("来源剧名") or "",
                    "query_text": source.get("完整字幕") or "",
                    "baseline_push_id": baseline.get("基准推送 drama_id") or "",
                    "baseline_original_id": baseline.get("基准原版剧ID") or "",
                    "baseline_original_name": baseline.get("基准原版剧名称") or "",
                    "source_row_in_733": index,
                }
            )

    print(
        json.dumps(
            {
                "selected_rows": len(coverage_rows),
                "source_counts": {
                    name: sum(1 for row in coverage_rows if source_by_id[str(row.get("视频 ID") or "")][0] == name)
                    for name in SOURCE_FILES
                },
                "total_query_chars": sum(
                    len(str(source_by_id[str(row.get("视频 ID") or "")][1].get("完整字幕") or ""))
                    for row in coverage_rows
                ),
                "output": str(OUTPUT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
