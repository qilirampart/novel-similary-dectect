from __future__ import annotations

import argparse
import json
import os
import shlex
from typing import Iterable

import paramiko


DEFAULT_ENV_FILE = "/opt/novel-similarity-service/shared/novel-similarity.env"
DEFAULT_RELEASE_DIR = "/opt/novel-similarity-service/current"
DEFAULT_VENV_PYTHON = "/opt/novel-similarity-service/shared/venv/bin/python"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source-ref", action="append", dest="source_refs", required=True)
    parser.add_argument("--mode", choices=("sequential", "concurrent", "both"), default="both")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--release-dir", default=DEFAULT_RELEASE_DIR)
    parser.add_argument("--python-bin", default=DEFAULT_VENV_PYTHON)
    return parser.parse_args()


def require_password(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"Missing password in environment variable: {env_name}")
    return value


def build_remote_python(task_id: str, source_refs: Iterable[str], mode: str) -> str:
    source_refs_json = json.dumps(list(source_refs), ensure_ascii=False)
    task_id_json = json.dumps(task_id, ensure_ascii=False)
    mode_json = json.dumps(mode, ensure_ascii=False)
    return f"""import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.config import SETTINGS
from service.candidate_slicing import slice_candidate_chapters
from service.compare_pipeline import ComparePipelineRequest
from service.detection_modes import get_detection_mode_preset
from service.fine_compare import compare_query_to_candidates
from service.global_retrieval import global_retrieve_candidates
from service.semantic_retrieval import (
    SemanticRetrievalError,
    _search_one_query_chunk,
    aggregate_semantic_hits,
    build_semantic_candidate_rows,
    embed_texts_remote,
    merge_recall_candidates,
    slice_query_semantic_chunks,
)

TASK_ID = {task_id_json}
SOURCE_REFS = {source_refs_json}
MODE = {mode_json}


def emit(event, **payload):
    print(json.dumps({{"event": event, **payload}}, ensure_ascii=False), flush=True)


def load_inputs():
    conn = sqlite3.connect(SETTINGS.business_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in SOURCE_REFS)
        rows = conn.execute(
            f\"\"\"
            SELECT i.item_order,
                   i.source_ref,
                   COALESCE(NULLIF(p.query_text, ''), i.query_text) AS query_text
              FROM compare_task_items i
              LEFT JOIN compare_task_item_payloads p
                ON p.result_id = i.result_id
             WHERE i.task_id = ?
               AND i.source_ref IN ({{placeholders}})
             ORDER BY i.item_order ASC
            \"\"\",
            [TASK_ID, *SOURCE_REFS],
        ).fetchall()
    finally:
        conn.close()
    return [
        {{
            "item_order": int(row["item_order"]),
            "source_ref": str(row["source_ref"]),
            "query_text": str(row["query_text"] or ""),
        }}
        for row in rows
    ]


def run_one(item):
    semantic_config = SETTINGS.build_semantic_config()
    preset = get_detection_mode_preset("rewrite")
    req = ComparePipelineRequest(
        db_path=SETTINGS.db_path,
        detection_mode="rewrite",
        query_text=item["query_text"],
        semantic_config=semantic_config,
        candidate_display_score_threshold=SETTINGS.candidate_display_score_threshold,
    )
    effective_merged_top_k = req.merged_top_k or preset.merged_top_k
    effective_compare_top_k = req.compare_top_k or preset.compare_top_k
    effective_top_k = req.top_k or preset.top_k
    started_at = time.perf_counter()
    stage_timings = {{}}
    thread_name = threading.current_thread().name

    def measure(name, fn):
        emit("stage_start", source_ref=item["source_ref"], stage=name, thread=thread_name)
        stage_start = time.perf_counter()
        value = fn()
        elapsed = time.perf_counter() - stage_start
        stage_timings[name] = round(elapsed, 4)
        emit("stage_end", source_ref=item["source_ref"], stage=name, seconds=round(elapsed, 4), thread=thread_name)
        return value

    lexical_payload = measure(
        "lexical_retrieve",
        lambda: global_retrieve_candidates(
            db_path=req.db_path,
            query_text=req.query_text,
            candidate_limit_per_index=req.candidate_limit_per_index,
            merged_top_k=effective_merged_top_k,
            ngram_size=req.ngram_size,
            max_query_ngrams=req.max_query_ngrams,
            weight_coarse=req.weight_coarse,
            seed_term_count=req.seed_term_count,
        ),
    )

    semantic_payload = {{
        "status": "disabled",
        "candidate_count": 0,
        "results": [],
        "error": "",
        "warnings": [],
    }}
    semantic_error = ""
    if preset.semantic_recall_enabled and semantic_config is not None:
        try:
            query_chunks = measure(
                "semantic_chunking",
                lambda: slice_query_semantic_chunks(
                    query_text=req.query_text,
                    window_size=semantic_config.query_window_size,
                    overlap_size=semantic_config.query_overlap_size,
                ),
            )
            query_vectors = measure(
                "semantic_embed",
                lambda: embed_texts_remote(
                    config=semantic_config,
                    texts=[str(chunk["text"]) for chunk in query_chunks],
                ),
            )

            def search_all():
                chapter_hits = []
                chunk_hits = []
                chapter_error = ""
                chunk_error = ""
                jobs = list(zip(query_chunks, query_vectors))
                if jobs:
                    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
                        futures = [
                            executor.submit(
                                _search_one_query_chunk,
                                query_chunk=query_chunk,
                                vector=vector,
                                effective=semantic_config,
                            )
                            for query_chunk, vector in jobs
                        ]
                        for future in futures:
                            hits_a, hits_b, err_a, err_b = future.result()
                            chapter_hits.extend(hits_a)
                            chunk_hits.extend(hits_b)
                            if err_a and not chapter_error:
                                chapter_error = err_a
                            if err_b and not chunk_error:
                                chunk_error = err_b
                return chapter_hits, chunk_hits, chapter_error, chunk_error

            chapter_hits, chunk_hits, chapter_error, chunk_error = measure("semantic_search", search_all)
            aggregated_hits = measure(
                "semantic_aggregate",
                lambda: aggregate_semantic_hits(chapter_hits=chapter_hits, chunk_hits=chunk_hits),
            )
            semantic_results = measure(
                "semantic_build",
                lambda: build_semantic_candidate_rows(
                    db_path=req.db_path,
                    aggregated_hits=aggregated_hits,
                    merged_top_k=semantic_config.merged_top_k,
                    source_name=f"{{semantic_config.chapter_collection}}+{{semantic_config.chunk_collection}}",
                ),
            )
            warnings = []
            if chapter_error:
                warnings.append(chapter_error)
            if chunk_error:
                warnings.append(chunk_error)
            semantic_payload = {{
                "status": "partial" if warnings else "ok",
                "candidate_count": len(semantic_results),
                "results": semantic_results,
                "error": " | ".join(warnings),
                "warnings": warnings,
            }}
        except SemanticRetrievalError as exc:
            semantic_error = str(exc)
            semantic_payload = {{
                "status": "error",
                "candidate_count": 0,
                "results": [],
                "error": semantic_error,
                "warnings": [],
            }}

    merged_candidates = measure(
        "merge_candidates",
        lambda: (
            merge_recall_candidates(
                lexical_results=list(lexical_payload["results"]),
                semantic_results=list(semantic_payload["results"]),
                merged_top_k=effective_merged_top_k,
            )
            if semantic_payload["status"] in {{"ok", "partial"}}
            else list(lexical_payload["results"])
        ),
    )
    coarse_candidates = merged_candidates[:effective_compare_top_k]
    chapter_uids = [int(item["chapter_uid"]) for item in coarse_candidates]
    sliced_candidates = measure(
        "slice_candidates",
        lambda: slice_candidate_chapters(
            db_path=req.db_path,
            chapter_uids=chapter_uids,
            window_size=req.window_size,
            step_size=req.step_size,
        ),
    )
    fine_results = measure(
        "fine_compare",
        lambda: compare_query_to_candidates(
            query_text=req.query_text,
            candidates=coarse_candidates,
            sliced_candidates=sliced_candidates,
            window_size=req.window_size,
            step_size=req.step_size,
            ngram_size=req.ngram_size,
        ),
    )
    total_seconds = round(time.perf_counter() - started_at, 4)
    top1 = fine_results[0] if fine_results else None
    result = {{
        "source_ref": item["source_ref"],
        "item_order": item["item_order"],
        "query_length": len(req.query_text),
        "thread": thread_name,
        "total_seconds": total_seconds,
        "stage_timings": stage_timings,
        "lexical_candidate_count": len(lexical_payload["results"]),
        "semantic_status": semantic_payload["status"],
        "semantic_candidate_count": int(semantic_payload["candidate_count"]),
        "semantic_error": semantic_payload["error"],
        "coarse_candidate_count": len(coarse_candidates),
        "sliced_candidate_count": len(sliced_candidates),
        "fine_result_count": len(fine_results[:effective_top_k]),
        "top1_book_name": "" if top1 is None else str(top1["book_name"]),
        "top1_chapter_name": "" if top1 is None else str(top1["chapter_name"]),
        "top1_score": None if top1 is None else round(float(top1["fine_score"]), 6),
    }}
    emit("item_done", **result)
    return result


def run_sequential(items):
    return [run_one(item) for item in items]


def run_concurrent(items):
    results = []
    with ThreadPoolExecutor(max_workers=len(items), thread_name_prefix="probe") as executor:
        futures = [executor.submit(run_one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: row["item_order"])
    return results


inputs = load_inputs()
emit("loaded_inputs", task_id=TASK_ID, count=len(inputs), source_refs=[item["source_ref"] for item in inputs])
if len(inputs) != len(SOURCE_REFS):
    raise SystemExit(f"Expected {{len(SOURCE_REFS)}} inputs, got {{len(inputs)}}")

payload = {{"task_id": TASK_ID, "mode": MODE, "results": {{}}}}
if MODE in ("sequential", "both"):
    seq_start = time.perf_counter()
    payload["results"]["sequential"] = run_sequential(inputs)
    payload["sequential_wall_seconds"] = round(time.perf_counter() - seq_start, 4)
if MODE in ("concurrent", "both"):
    conc_start = time.perf_counter()
    payload["results"]["concurrent"] = run_concurrent(inputs)
    payload["concurrent_wall_seconds"] = round(time.perf_counter() - conc_start, 4)
emit("final_summary", **payload)
"""


def build_remote_command(args: argparse.Namespace) -> str:
    python_source = build_remote_python(
        task_id=args.task_id,
        source_refs=args.source_refs,
        mode=args.mode,
    )
    quoted_env_file = shlex.quote(args.env_file)
    quoted_release_dir = shlex.quote(args.release_dir)
    quoted_python_bin = shlex.quote(args.python_bin)
    quoted_timeout = shlex.quote(str(max(int(args.timeout_seconds), 1)))
    return (
        "bash -lc "
        + shlex.quote(
            "set -a; "
            f"source {quoted_env_file}; "
            "set +a; "
            f"cd {quoted_release_dir}; "
            f"/usr/bin/timeout {quoted_timeout}s {quoted_python_bin} - <<'PY'\n"
            f"{python_source}\n"
            "PY"
        )
    )


def main() -> int:
    args = parse_args()
    password = require_password(args.password_env)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.host,
        username=args.user,
        password=password,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    command = build_remote_command(args)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=max(args.timeout_seconds + 30, 60))
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()

    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip())
    if exit_code != 0:
        raise SystemExit(exit_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
