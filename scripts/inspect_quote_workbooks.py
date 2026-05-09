from pathlib import Path
import json

import pandas as pd


FILES = {
    "quotes": Path(r"C:\Users\psk13\Desktop\实验室任务\aps\02_功能包装材料论文项目\03_检索与写作\01_主稿\补充图片\漫剧台词表.xlsx"),
    "target77": Path(r"E:\点众\小说库相似度比对服务工作区\数据现状说明\01_中台可查_且数据库已有\01_修正版77条结果.xlsx"),
    "old10": Path(r"E:\点众\小说库相似度比对服务工作区\dialogue_quote_compare_results.xlsx"),
}


def preview_df(df: pd.DataFrame, limit: int = 2) -> list[dict]:
    rows = []
    for row in df.head(limit).to_dict(orient="records"):
        clean = {}
        for k, v in row.items():
            if pd.isna(v):
                clean[str(k)] = None
            else:
                clean[str(k)] = str(v)[:200]
        rows.append(clean)
    return rows


def main() -> None:
    result = {}
    for name, path in FILES.items():
        entry = {"path": str(path), "exists": path.exists()}
        if path.exists():
            xl = pd.ExcelFile(path)
            sheets = []
            for sheet_name in xl.sheet_names:
                df = pd.read_excel(path, sheet_name=sheet_name)
                sheets.append(
                    {
                        "sheet": sheet_name,
                        "rows": len(df),
                        "columns": [str(c) for c in df.columns],
                        "preview": preview_df(df),
                    }
                )
            entry["sheets"] = sheets
        result[name] = entry
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
