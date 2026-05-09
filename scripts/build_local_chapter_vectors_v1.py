from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from service.local_semantic_retrieval import load_local_sentence_model, resolve_default_local_model_path
from v2_common import connect_db, now_ts


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_where_sql(dataset_key: str, canonical_only: bool, start_after_chapter_uid: int) -> tuple[str, list[object]]:
    sql = """
        FROM chapters c
        JOIN chapter_contents cc ON cc.chapter_uid = c.chapter_uid
       WHERE cc.content_clean IS NOT NULL
         AND cc.content_clean != ''
    """
    params: list[object] = []
    if dataset_key:
        sql += " AND c.dataset_key = ?"
        params.append(dataset_key)
    if start_after_chapter_uid > 0:
        sql += " AND c.chapter_uid > ?"
        params.append(start_after_chapter_uid)
    if canonical_only:
        sql += """
         AND NOT EXISTS (
                SELECT 1
                  FROM chapter_exact_dedup_members m
                 WHERE m.chapter_uid = c.chapter_uid
                   AND m.is_canonical = 0
         )
        """
    return sql, params


def fetch_total_count(conn: object, dataset_key: str, canonical_only: bool, limit: int) -> int:
    where_sql, params = build_where_sql(dataset_key, canonical_only, start_after_chapter_uid=0)
    count_sql = "SELECT COUNT(*) " + where_sql
    total = int(conn.execute(count_sql, params).fetchone()[0])
    if limit > 0:
        return min(total, limit)
    return total


def build_select_sql(
    *,
    dataset_key: str,
    canonical_only: bool,
    start_after_chapter_uid: int,
    remaining_limit: int,
) -> tuple[str, list[object]]:
    where_sql, params = build_where_sql(dataset_key, canonical_only, start_after_chapter_uid)
    sql = """
        SELECT c.chapter_uid, cc.content_clean
    """ + where_sql + """
        ORDER BY c.chapter_uid
    """
    if remaining_limit > 0:
        sql += f" LIMIT {remaining_limit}"
    return sql, params


def estimate_eta_seconds(started_at: float, processed_count: int, total_count: int) -> int:
    if processed_count <= 0 or total_count <= 0 or processed_count >= total_count:
        return 0
    elapsed = time.time() - started_at
    return int((elapsed / processed_count) * (total_count - processed_count))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--canonical-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fetch-batch-size", type=int, default=256)
    parser.add_argument("--encode-batch-size", type=int, default=16)
    parser.add_argument(
        "--output-dir",
        default="data/semantic_local_index/chapter_bge_small_zh_v1_5_all_canonical",
    )
    parser.add_argument("--model-path", default="")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--progress-json", default="logs/local_chapter_vectors_progress.json")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.fetch_batch_size <= 0:
        raise ValueError("--fetch-batch-size must be > 0")
    if args.encode_batch_size <= 0:
        raise ValueError("--encode-batch-size must be > 0")

    output_dir = Path(args.output_dir)
    manifest_path = output_dir / "manifest.json"
    vector_file = output_dir / "chapter_vectors.f32"
    chapter_uid_file = output_dir / "chapter_uids.i64"
    progress_path = Path(args.progress_json)
    model_path = args.model_path or resolve_default_local_model_path()
    if not model_path:
        raise FileNotFoundError(
            "No local embedding model path found. Use --model-path or NOVEL_LOCAL_EMBED_MODEL_PATH."
        )

    model = load_local_sentence_model(model_path=model_path, device=args.device)
    vector_dim = int(model.get_sentence_embedding_dimension())
    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        total_count = fetch_total_count(
            conn=conn,
            dataset_key=args.dataset_key,
            canonical_only=args.canonical_only,
            limit=args.limit,
        )
        if total_count <= 0:
            raise ValueError("No chapters matched the current build filters.")

        if args.overwrite:
            for path in (manifest_path, vector_file, chapter_uid_file, progress_path):
                if path.exists():
                    path.unlink()

        output_dir.mkdir(parents=True, exist_ok=True)
        processed_count = 0
        last_processed_chapter_uid = 0
        resumed = False

        if manifest_path.exists() and vector_file.exists() and chapter_uid_file.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if int(manifest.get("row_count", 0)) != total_count:
                raise ValueError(
                    "Existing local semantic index row_count does not match current selection. "
                    "Use --overwrite to rebuild."
                )
            if int(manifest.get("vector_dim", 0)) != vector_dim:
                raise ValueError(
                    "Existing local semantic index vector_dim does not match current model. "
                    "Use --overwrite to rebuild."
                )
            if progress_path.exists():
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                processed_count = int(progress.get("processed_count", 0))
                last_processed_chapter_uid = int(progress.get("last_processed_chapter_uid", 0))
                resumed = processed_count > 0
        else:
            write_json(
                manifest_path,
                {
                    "status": "running",
                    "created_at": now_ts(),
                    "updated_at": now_ts(),
                    "dataset_key": args.dataset_key,
                    "canonical_only": bool(args.canonical_only),
                    "limit": int(args.limit),
                    "row_count": total_count,
                    "vector_dim": vector_dim,
                    "vector_file": vector_file.name,
                    "chapter_uid_file": chapter_uid_file.name,
                    "model_path": model_path,
                    "device": args.device,
                },
            )

        vectors = np.memmap(
            vector_file,
            dtype=np.float32,
            mode="r+" if vector_file.exists() else "w+",
            shape=(total_count, vector_dim),
        )
        chapter_uids = np.memmap(
            chapter_uid_file,
            dtype=np.int64,
            mode="r+" if chapter_uid_file.exists() else "w+",
            shape=(total_count,),
        )

        if processed_count >= total_count:
            write_json(
                progress_path,
                {
                    "status": "completed",
                    "processed_count": processed_count,
                    "total_count": total_count,
                    "pct": 1.0,
                    "last_processed_chapter_uid": last_processed_chapter_uid,
                    "updated_at": now_ts(),
                    "eta_seconds": 0,
                    "resumed": resumed,
                },
            )
            print(f"OK already_completed={processed_count}")
            print(f"OK output_dir={output_dir}")
            return

        remaining_limit = total_count - processed_count
        sql, params = build_select_sql(
            dataset_key=args.dataset_key,
            canonical_only=args.canonical_only,
            start_after_chapter_uid=last_processed_chapter_uid,
            remaining_limit=remaining_limit,
        )
        cursor = conn.execute(sql, params)

        started_at = time.time()
        write_offset = processed_count
        while True:
            rows = cursor.fetchmany(args.fetch_batch_size)
            if not rows:
                break

            texts = [str(content_clean) for _, content_clean in rows]
            embeddings = model.encode(
                texts,
                batch_size=args.encode_batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            embeddings = np.asarray(embeddings, dtype=np.float32)
            batch_count = len(rows)
            end_offset = write_offset + batch_count

            vectors[write_offset:end_offset] = embeddings
            chapter_uids[write_offset:end_offset] = np.asarray(
                [int(chapter_uid) for chapter_uid, _ in rows],
                dtype=np.int64,
            )
            vectors.flush()
            chapter_uids.flush()

            write_offset = end_offset
            processed_count = write_offset
            last_processed_chapter_uid = int(rows[-1][0])
            eta_seconds = estimate_eta_seconds(
                started_at=started_at,
                processed_count=processed_count,
                total_count=total_count,
            )
            progress_payload = {
                "status": "running" if processed_count < total_count else "completed",
                "processed_count": processed_count,
                "total_count": total_count,
                "pct": processed_count / total_count,
                "last_processed_chapter_uid": last_processed_chapter_uid,
                "updated_at": now_ts(),
                "eta_seconds": eta_seconds,
                "resumed": resumed,
                "output_dir": str(output_dir),
                "model_path": model_path,
                "device": args.device,
            }
            write_json(progress_path, progress_payload)
            write_json(
                manifest_path,
                {
                    "status": progress_payload["status"],
                    "created_at": json.loads(manifest_path.read_text(encoding="utf-8")).get(
                        "created_at",
                        now_ts(),
                    ),
                    "updated_at": now_ts(),
                    "dataset_key": args.dataset_key,
                    "canonical_only": bool(args.canonical_only),
                    "limit": int(args.limit),
                    "row_count": total_count,
                    "vector_dim": vector_dim,
                    "vector_file": vector_file.name,
                    "chapter_uid_file": chapter_uid_file.name,
                    "model_path": model_path,
                    "device": args.device,
                },
            )
            print(
                f"PROGRESS processed={processed_count}/{total_count} "
                f"pct={processed_count / total_count:.4f} "
                f"last_chapter_uid={last_processed_chapter_uid} "
                f"eta_seconds={eta_seconds}"
            )
    finally:
        conn.close()

    print(f"OK output_dir={output_dir}")
    print(f"OK total_count={total_count}")
    print(f"OK vector_dim={vector_dim}")
    print(f"OK progress_json={progress_path}")


if __name__ == "__main__":
    main()
