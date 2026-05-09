from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_global_retrieve_smoke() -> None:
    query_file = ROOT / "data_samples" / "query_smoketest_ch1.txt"
    if not query_file.exists():
        query_file.write_text(
            "交换完戒指，刘昕阳一转头看到了突然出现的人，愣了愣问道：你怎么会在这里？",
            encoding="utf-8",
        )

    out_file = ROOT / "data_samples" / "global_retrieve_smoketest.json"
    if out_file.exists():
        out_file.unlink()

    completed = subprocess.run(
        [
            "python",
            "scripts/global_retrieve_v1.py",
            "--db",
            "data/novel_similarity_v2.sqlite3",
            "--query-file",
            str(query_file),
            "--merged-top-k",
            "10",
            "--json-out",
            str(out_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "OK target_count=" in completed.stdout
    assert out_file.exists()

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["target_count"] >= 2
    assert isinstance(payload["index_results"], list)
    assert isinstance(payload["results"], list)
