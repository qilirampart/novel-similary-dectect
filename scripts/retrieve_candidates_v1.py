from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from v2_common import connect_db


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NON_TEXT_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def validate_ident(value: str) -> str:
    if not IDENT_RE.match(value):
        raise ValueError(f"Invalid SQLite identifier: {value!r}")
    return value


def normalize_retrieval_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def normalize_scoring_text(text: str) -> str:
    text = normalize_retrieval_text(text).lower()
    text = NON_TEXT_RE.sub("", text)
    return text


def ordered_unique_ngrams(text: str, n: int, max_terms: int = 0) -> list[str]:
    if not text:
        return []
    if len(text) < n:
        return [text]

    seen: set[str] = set()
    grams: list[str] = []
    for idx in range(len(text) - n + 1):
        gram = text[idx : idx + n]
        if gram in seen:
            continue
        seen.add(gram)
        grams.append(gram)
        if max_terms > 0 and len(grams) >= max_terms:
            break
    return grams


def build_match_query(text: str, n: int, max_terms: int) -> tuple[str, str, list[str]]:
    scoring_text = normalize_scoring_text(text)
    grams = ordered_unique_ngrams(scoring_text, n, max_terms)
    if not grams:
        raise ValueError("Query is empty after normalization")
    if len(scoring_text) < n:
        raise ValueError(
            f"Query is too short for trigram retrieval after normalization: len={len(scoring_text)}"
        )

    match_query = " OR ".join(f'"{gram}"' for gram in grams)
    return match_query, scoring_text, grams


def overlap_score(query_grams: set[str], candidate_text: str, n: int) -> float:
    if not query_grams:
        return 0.0
    candidate_grams = set(ordered_unique_ngrams(normalize_scoring_text(candidate_text), n))
    if not candidate_grams:
        return 0.0
    return len(query_grams & candidate_grams) / len(query_grams)


def normalize_bm25_scores(values: list[float]) -> list[float]:
    if not values:
        return []

    raw = [-value for value in values]
    lo = min(raw)
    hi = max(raw)
    if hi - lo < 1e-12:
        if len(values) == 1:
            return [1.0]
        return [1.0 - (idx / (len(values) - 1)) for idx in range(len(values))]
    return [(value - lo) / (hi - lo) for value in raw]


def load_query_text(query_text: str, query_file: str) -> str:
    if query_text:
        return query_text
    if query_file:
        return Path(query_file).read_text(encoding="utf-8")
    raise ValueError("Either --query-text or --query-file is required")


def ensure_fts_table_exists(conn: sqlite3.Connection, table_name: str) -> None:
    rows = conn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type = 'table'
           AND name = ?
        """,
        (table_name,),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"FTS table {table_name!r} does not exist. Build it first with build_chapter_fts_v2.py"
        )


def get_index_scope(conn: sqlite3.Connection, table_name: str) -> str | None:
    rows = conn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type = 'table'
           AND name = 'retrieval_fts_indexes'
        """
    ).fetchall()
    if not rows:
        return None

    meta = conn.execute(
        """
        SELECT dataset_key
          FROM retrieval_fts_indexes
         WHERE table_name = ?
        """,
        (table_name,),
    ).fetchone()
    if not meta:
        return None
    return meta[0]


def ensure_vocab_table(conn: sqlite3.Connection, table_name: str) -> str:
    vocab_name = validate_ident(f"{table_name}_vocab")
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {vocab_name}
        USING fts5vocab({table_name}, 'row')
        """
    )
    return vocab_name


def fetch_doc_freqs(
    conn: sqlite3.Connection,
    vocab_name: str,
    grams: list[str],
) -> dict[str, int]:
    if not grams:
        return {}
    placeholders = ",".join("?" for _ in grams)
    rows = conn.execute(
        f"SELECT term, doc FROM {vocab_name} WHERE term IN ({placeholders})",
        grams,
    ).fetchall()
    return {str(term): int(doc) for term, doc in rows}


def build_seed_grams(
    doc_freqs: dict[str, int],
    grams: list[str],
    seed_term_count: int,
) -> list[tuple[str, int]]:
    ranked = [(gram, doc_freqs.get(gram, 10**9)) for gram in grams]
    ranked.sort(key=lambda item: (item[1], item[0]))
    return ranked[:seed_term_count]


def fetch_candidate_pool(
    conn: sqlite3.Connection,
    table_name: str,
    dataset_key: str,
    index_scope: str | None,
    seed_grams: list[tuple[str, int]],
    pool_limit: int,
) -> list[tuple[int, int, float]]:
    if not seed_grams:
        return []

    parts: list[str] = []
    params: list[object] = []
    use_dataset_filter = bool(dataset_key) and dataset_key != index_scope
    for gram, doc_freq in seed_grams:
        if use_dataset_filter:
            sql = f"""
                SELECT c.chapter_uid AS chapter_uid, ? AS seed_weight
                  FROM {table_name}
                  JOIN chapters c
                    ON c.chapter_uid = {table_name}.rowid
                 WHERE {table_name} MATCH ?
            """
            params.extend([1.0 / max(doc_freq, 1), f'"{gram}"'])
            sql += " AND c.dataset_key = ?"
            params.append(dataset_key)
            parts.append(sql)
            continue

        sql = f"""
            SELECT rowid AS chapter_uid, ? AS seed_weight
              FROM {table_name}
             WHERE {table_name} MATCH ?
        """
        params.extend([1.0 / max(doc_freq, 1), f'"{gram}"'])
        parts.append(sql)

    sql = (
        "WITH hits AS ("
        + " UNION ALL ".join(parts)
        + """
        )
        SELECT chapter_uid,
               count(*) AS seed_hit_count,
               sum(seed_weight) AS seed_hit_weight
          FROM hits
         GROUP BY chapter_uid
         ORDER BY seed_hit_count DESC,
                  seed_hit_weight DESC,
                  chapter_uid ASC
         LIMIT ?
        """
    )
    params.append(pool_limit)
    rows = conn.execute(sql, params).fetchall()
    return [(int(chapter_uid), int(hit_count), float(hit_weight)) for chapter_uid, hit_count, hit_weight in rows]


def fetch_candidate_metadata(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
) -> dict[int, dict[str, object]]:
    if not candidate_ids:
        return {}

    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"""
        SELECT c.chapter_uid,
               c.dataset_key,
               b.book_ext_id,
               b.book_name,
               c.chapter_ext_id,
               c.chapter_name,
               cc.content_retrieval
          FROM chapters c
          JOIN books b
            ON b.book_uid = c.book_uid
          JOIN chapter_contents cc
            ON cc.chapter_uid = c.chapter_uid
         WHERE c.chapter_uid IN ({placeholders})
        """,
        candidate_ids,
    ).fetchall()
    return {
        int(chapter_uid): {
            "chapter_uid": int(chapter_uid),
            "dataset_key": dataset_key,
            "book_ext_id": book_ext_id,
            "book_name": book_name,
            "chapter_ext_id": chapter_ext_id,
            "chapter_name": chapter_name,
            "content_retrieval": content_retrieval,
        }
        for (
            chapter_uid,
            dataset_key,
            book_ext_id,
            book_name,
            chapter_ext_id,
            chapter_name,
            content_retrieval,
        ) in rows
    }


def retrieve_candidates(
    conn: sqlite3.Connection,
    table_name: str,
    query_text: str,
    dataset_key: str = "",
    candidate_limit: int = 200,
    ngram_size: int = 3,
    max_query_ngrams: int = 120,
    weight_coarse: float = 0.25,
    seed_term_count: int = 12,
) -> dict[str, object]:
    if candidate_limit <= 0:
        raise ValueError("candidate_limit must be > 0")
    if ngram_size <= 0:
        raise ValueError("ngram_size must be > 0")
    if seed_term_count <= 0:
        raise ValueError("seed_term_count must be > 0")
    if not 0.0 <= weight_coarse <= 1.0:
        raise ValueError("weight_coarse must be between 0 and 1")

    validated_table = validate_ident(table_name)
    _, scoring_query_text, query_grams_list = build_match_query(
        query_text,
        n=ngram_size,
        max_terms=max_query_ngrams,
    )
    query_grams = set(query_grams_list)

    ensure_fts_table_exists(conn, validated_table)
    index_scope = get_index_scope(conn, validated_table)
    if dataset_key and index_scope and dataset_key != index_scope:
        return {
            "query_text": query_text,
            "query_chars_raw": len(query_text),
            "query_chars_scoring": len(scoring_query_text),
            "query_ngrams": len(query_grams),
            "fts_terms": len(query_grams_list),
            "seed_terms": 0,
            "candidate_count": 0,
            "results": [],
        }
    vocab_name = ensure_vocab_table(conn, validated_table)
    doc_freqs = fetch_doc_freqs(conn, vocab_name, query_grams_list)
    seed_grams = build_seed_grams(doc_freqs, query_grams_list, seed_term_count)
    candidate_pool = fetch_candidate_pool(
        conn=conn,
        table_name=validated_table,
        dataset_key=dataset_key,
        index_scope=index_scope,
        seed_grams=seed_grams,
        pool_limit=candidate_limit,
    )
    if not candidate_pool:
        return {
            "query_text": query_text,
            "query_chars_raw": len(query_text),
            "query_chars_scoring": len(scoring_query_text),
            "query_ngrams": len(query_grams),
            "fts_terms": len(query_grams_list),
            "seed_terms": len(seed_grams),
            "candidate_count": 0,
            "results": [],
        }

    candidate_ids = [chapter_uid for chapter_uid, _, _ in candidate_pool]
    metadata_map = fetch_candidate_metadata(conn, candidate_ids)
    max_hit_count = max(hit_count for _, hit_count, _ in candidate_pool)
    max_hit_weight = max(hit_weight for _, _, hit_weight in candidate_pool)

    results = []
    for chapter_uid, hit_count, hit_weight in candidate_pool:
        meta = metadata_map.get(chapter_uid)
        if not meta:
            continue
        overlap = overlap_score(query_grams, str(meta["content_retrieval"]), ngram_size)
        coarse_score = (0.7 * (hit_count / max_hit_count)) + (
            0.3 * (hit_weight / max_hit_weight if max_hit_weight else 0.0)
        )
        final_score = (weight_coarse * coarse_score) + ((1.0 - weight_coarse) * overlap)
        results.append(
            {
                "chapter_uid": chapter_uid,
                "dataset_key": meta["dataset_key"],
                "book_ext_id": meta["book_ext_id"],
                "book_name": meta["book_name"],
                "chapter_ext_id": meta["chapter_ext_id"],
                "chapter_name": meta["chapter_name"],
                "bm25_score": 0.0,
                "bm25_norm": 0.0,
                "seed_hit_count": hit_count,
                "seed_hit_weight": hit_weight,
                "coarse_score": coarse_score,
                "ngram_score": overlap,
                "final_score": final_score,
            }
        )

    results.sort(
        key=lambda item: (
            -item["final_score"],
            -item["ngram_score"],
            -item["seed_hit_count"],
            -item["seed_hit_weight"],
            item["chapter_uid"],
        )
    )
    return {
        "query_text": query_text,
        "query_chars_raw": len(query_text),
        "query_chars_scoring": len(scoring_query_text),
        "query_ngrams": len(query_grams),
        "fts_terms": len(query_grams_list),
        "seed_terms": len(seed_grams),
        "candidate_count": len(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/novel_similarity_v2.sqlite3")
    parser.add_argument("--table", default="chapter_fts_v2")
    parser.add_argument("--dataset-key", default="")
    parser.add_argument("--query-text", default="")
    parser.add_argument("--query-file", default="")
    parser.add_argument("--candidate-limit", type=int, default=200)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--ngram-size", type=int, default=3)
    parser.add_argument("--max-query-ngrams", type=int, default=120)
    parser.add_argument(
        "--weight-coarse",
        "--weight-bm25",
        dest="weight_coarse",
        type=float,
        default=0.25,
        help="Weight of coarse seed-hit score in final_score",
    )
    parser.add_argument(
        "--seed-term-count",
        type=int,
        default=12,
        help="How many rare query 3-grams to use for candidate pooling",
    )
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    if args.candidate_limit <= 0:
        raise ValueError("--candidate-limit must be > 0")
    if args.top_k <= 0:
        raise ValueError("--top-k must be > 0")
    if args.ngram_size <= 0:
        raise ValueError("--ngram-size must be > 0")
    if args.seed_term_count <= 0:
        raise ValueError("--seed-term-count must be > 0")
    if not 0.0 <= args.weight_coarse <= 1.0:
        raise ValueError("--weight-coarse must be between 0 and 1")

    query_text = load_query_text(args.query_text, args.query_file)

    conn = connect_db(args.db)
    conn.row_factory = None
    try:
        payload = retrieve_candidates(
            conn=conn,
            table_name=args.table,
            query_text=query_text,
            dataset_key=args.dataset_key,
            candidate_limit=args.candidate_limit,
            ngram_size=args.ngram_size,
            max_query_ngrams=args.max_query_ngrams,
            weight_coarse=args.weight_coarse,
            seed_term_count=args.seed_term_count,
        )
    finally:
        conn.close()

    candidate_count = int(payload["candidate_count"])
    results = list(payload["results"])
    if not results:
        print("OK candidates=0")
        print(f"OK query_ngrams={payload['query_ngrams']}")
        return

    top_results = results[: args.top_k]

    print(f"OK candidates={candidate_count}")
    print(f"OK query_chars_raw={payload['query_chars_raw']}")
    print(f"OK query_chars_scoring={payload['query_chars_scoring']}")
    print(f"OK query_ngrams={payload['query_ngrams']}")
    print(f"OK fts_terms={payload['fts_terms']}")
    print(f"OK seed_terms={payload['seed_terms']}")

    for rank, item in enumerate(top_results, start=1):
        print(
            "RESULT "
            f"rank={rank} "
            f"final_score={item['final_score']:.6f} "
            f"coarse_score={item['coarse_score']:.6f} "
            f"seed_hit_count={item['seed_hit_count']} "
            f"seed_hit_weight={item['seed_hit_weight']:.6f} "
            f"ngram_score={item['ngram_score']:.6f} "
            f"dataset_key={item['dataset_key']} "
            f"book_ext_id={item['book_ext_id']} "
            f"book_name={json.dumps(item['book_name'], ensure_ascii=False)} "
            f"chapter_ext_id={item['chapter_ext_id']} "
            f"chapter_uid={item['chapter_uid']} "
            f"chapter_name={json.dumps(item['chapter_name'], ensure_ascii=False)}"
        )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "query_text": query_text,
                    "query_chars_scoring": payload["query_chars_scoring"],
                    "query_ngrams": payload["query_ngrams"],
                    "results": top_results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"OK json_out={args.json_out}")


if __name__ == "__main__":
    main()
