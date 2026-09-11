from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_global_compare_smoke() -> None:
    query_file = ROOT / "data_samples" / "query_smoketest_ch1.txt"
    if not query_file.exists():
        query_file.write_text(
            "交换完戒指，刘昕阳一转头看到了突然出现的人，愣了愣问道：你怎么会在这里？",
            encoding="utf-8",
        )

    out_file = ROOT / "data_samples" / "global_compare_smoketest.json"
    if out_file.exists():
        out_file.unlink()
    csv_file = ROOT / "data_samples" / "global_compare_smoketest.csv"
    if csv_file.exists():
        csv_file.unlink()

    completed = subprocess.run(
        [
            "python",
            "scripts/run_global_compare_v1.py",
            "--detection-mode",
            "rewrite",
            "--db",
            "data/novel_similarity_v2.sqlite3",
            "--query-file",
            str(query_file),
            "--merged-top-k",
            "10",
            "--compare-top-k",
            "6",
            "--top-k",
            "3",
            "--json-out",
            str(out_file),
            "--csv-out",
            str(csv_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "OK detection_mode=rewrite" in completed.stdout
    assert "OK fine_results=" in completed.stdout
    assert out_file.exists()
    assert csv_file.exists()

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["detection_mode"] == "rewrite"
    assert payload["coarse"]["target_count"] >= 2
    assert payload["capabilities"]["rewrite_detection_enabled"] is True
    assert payload["rewrite_detection"]["enabled"] is True
    assert payload["rewrite_detection"]["status"] in {
        "semantic_ready",
        "fallback_lexical_only",
        "fallback_semantic_timeout",
    }
    assert "semantic" in payload["coarse"]
    assert payload["fine"]["compared_candidate_count"] > 0
    assert isinstance(payload["fine"]["results"], list)
    assert payload["fine"]["results"]
    best_match = payload["fine"]["results"][0]["best_match"]
    assert best_match["candidate_window_count"] > 0
    assert best_match["candidate_start_offset"] >= 0
    assert isinstance(best_match["candidate_text"], str)
    assert "review_rows" in payload["fine"]
    assert payload["fine"]["review_rows"]
    assert payload["fine"]["results"][0]["review_label"] in {"强证据", "中证据", "弱证据"}
