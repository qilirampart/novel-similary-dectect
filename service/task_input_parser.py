from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import re
from typing import Iterable


TEXT_HEADER_CANDIDATES = {
    "query_text",
    "text",
    "content",
    "subtitle_text",
    "subtitle",
    "文案",
    "文本",
    "内容",
    "字幕",
    "待检测文本",
    "待检测文案",
}

SOURCE_REF_HEADER_CANDIDATES = {
    "id",
    "source_id",
    "source_ref",
    "row_id",
    "line_no",
    "序号",
    "编号",
    "标题",
    "title",
    "name",
    "名称",
}


@dataclass(frozen=True)
class ParsedTaskInput:
    item_order: int
    source_ref: str
    query_text: str


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value or "").strip().lower()


def _load_text_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    blocks = [part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()]
    if len(blocks) > 1:
        return blocks
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) > 1 and max(len(line) for line in lines) <= 300:
        return lines
    return [normalized]


def _detect_text_column(headers: Iterable[str]) -> str:
    header_list = [header for header in headers if header is not None]
    if not header_list:
        raise ValueError("No headers found in batch file")
    if len(header_list) == 1:
        return header_list[0]
    normalized_map = {_normalize_header(header): header for header in header_list}
    for candidate in TEXT_HEADER_CANDIDATES:
        found = normalized_map.get(_normalize_header(candidate))
        if found:
            return found
    for header in header_list:
        if _normalize_header(header) not in {
            _normalize_header(value) for value in SOURCE_REF_HEADER_CANDIDATES
        }:
            return header
    return header_list[0]


def _detect_source_ref_column(headers: Iterable[str], text_column: str) -> str | None:
    normalized_map = {_normalize_header(header): header for header in headers if header is not None}
    for candidate in SOURCE_REF_HEADER_CANDIDATES:
        found = normalized_map.get(_normalize_header(candidate))
        if found and found != text_column:
            return found
    return None


def _build_rows_from_dicts(rows: list[dict[str, str]]) -> list[ParsedTaskInput]:
    if not rows:
        return []
    text_column = _detect_text_column(rows[0].keys())
    source_ref_column = _detect_source_ref_column(rows[0].keys(), text_column=text_column)
    parsed: list[ParsedTaskInput] = []
    for row_index, row in enumerate(rows, start=1):
        query_text = str(row.get(text_column, "") or "").strip()
        if not query_text:
            continue
        source_ref = ""
        if source_ref_column:
            source_ref = str(row.get(source_ref_column, "") or "").strip()
        if not source_ref:
            source_ref = f"row_{row_index}"
        parsed.append(
            ParsedTaskInput(
                item_order=len(parsed) + 1,
                source_ref=source_ref,
                query_text=query_text,
            )
        )
    return parsed


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    sample = path.read_text(encoding="utf-8-sig", errors="replace")[:4096]
    delimiter = ","
    if path.suffix.lower() == ".tsv":
        delimiter = "\t"
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        return list(reader)


def _load_xlsx_rows(path: Path) -> list[dict[str, str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Parsing .xlsx requires openpyxl to be installed") from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return []
    headers = [str(value).strip() if value is not None else "" for value in rows[0]]
    data_rows: list[dict[str, str]] = []
    for values in rows[1:]:
        row = {
            headers[index]: "" if value is None else str(value)
            for index, value in enumerate(values)
            if index < len(headers) and headers[index]
        }
        data_rows.append(row)
    return data_rows


def parse_task_input_file(path: str | Path) -> list[ParsedTaskInput]:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return [
            ParsedTaskInput(item_order=index, source_ref=f"block_{index}", query_text=text)
            for index, text in enumerate(_load_text_blocks(file_path), start=1)
            if text.strip()
        ]
    if suffix in {".csv", ".tsv"}:
        return _build_rows_from_dicts(_load_csv_rows(file_path))
    if suffix == ".xlsx":
        return _build_rows_from_dicts(_load_xlsx_rows(file_path))
    raise ValueError(f"Unsupported task input file type: {suffix}")
