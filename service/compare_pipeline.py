from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from service.candidate_slicing import slice_candidate_chapters
from service.detection_modes import get_detection_mode_preset
from service.fine_compare import compare_query_to_candidates
from service.global_retrieval import global_retrieve_candidates
from service.semantic_retrieval import (
    SemanticRetrievalConfig,
    SemanticRetrievalError,
    merge_recall_candidates,
    semantic_retrieve_candidates,
)


@dataclass(frozen=True)
class ComparePipelineRequest:
    db_path: str
    detection_mode: str
    query_text: str
    candidate_limit_per_index: int = 200
    merged_top_k: int | None = None
    compare_top_k: int | None = None
    top_k: int | None = None
    window_size: int = 200
    step_size: int = 50
    ngram_size: int = 3
    max_query_ngrams: int = 120
    weight_coarse: float = 0.25
    seed_term_count: int = 12
    disable_semantic_recall: bool = False
    semantic_config: SemanticRetrievalConfig | None = None
    candidate_display_score_threshold: float | None = None


def build_review_rows(
    query_text: str,
    fine_results: list[dict[str, object]],
    detection_mode: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in fine_results:
        best_match = dict(item["best_match"])
        rows.append(
            {
                "detection_mode": detection_mode,
                "fine_rank": item["fine_rank"],
                "review_label": item["review_label"],
                "confidence_label": item["confidence_label"],
                "fine_score": round(float(item["fine_score"]), 6),
                "coarse_rank": item["coarse_rank"],
                "coarse_final_score": round(float(item["coarse_final_score"]), 6),
                "dataset_key": item["dataset_key"],
                "book_ext_id": item["book_ext_id"],
                "book_name": item["book_name"],
                "chapter_uid": item["chapter_uid"],
                "chapter_ext_id": item["chapter_ext_id"],
                "chapter_name": item["chapter_name"],
                "candidate_window_order": best_match["candidate_window_order"],
                "candidate_start_offset": best_match["candidate_start_offset"],
                "candidate_end_offset": best_match["candidate_end_offset"],
                "exact_substring_hit": bool(best_match["exact_substring_hit"]),
                "longest_match_len": best_match["longest_match_len"],
                "longest_match_ratio": round(float(best_match["longest_match_ratio"]), 6),
                "ngram_recall": round(float(best_match["ngram_recall"]), 6),
                "ngram_precision": round(float(best_match["ngram_precision"]), 6),
                "jaccard": round(float(best_match["jaccard"]), 6),
                "sequence_ratio": round(float(best_match["sequence_ratio"]), 6),
                "matched_substring": best_match["matched_substring"],
                "query_text_preview": best_match["query_text_preview"],
                "candidate_text_preview": best_match["candidate_text_preview"],
                "query_text": query_text,
                "candidate_text": best_match["candidate_text"],
            }
        )
    return rows


def build_reuse_detection_section(
    fine_results: list[dict[str, object]],
    top_k: int,
) -> dict[str, object]:
    primary_results = fine_results[:top_k]
    strong_evidence_results = [item for item in fine_results if item["review_label"] == "强证据"][:top_k]
    return {
        "enabled": True,
        "result_count": len(primary_results),
        "strong_evidence_count_in_pool": sum(1 for item in fine_results if item["review_label"] == "强证据"),
        "results": primary_results,
        "strong_evidence_results": strong_evidence_results,
    }


def build_rewrite_detection_section(
    fine_results: list[dict[str, object]],
    top_k: int,
    semantic_recall_enabled: bool,
    semantic_recall_status: str,
    semantic_recall_message: str,
    semantic_candidate_count: int,
) -> dict[str, object]:
    suspicious_results: list[dict[str, object]] = []
    for item in fine_results:
        best_match = item["best_match"]
        if bool(best_match["exact_substring_hit"]) and float(best_match["longest_match_ratio"]) >= 0.95:
            continue
        suspicious_results.append(item)
        if len(suspicious_results) >= top_k:
            break

    return {
        "enabled": True,
        "status": semantic_recall_status,
        "semantic_recall_enabled": semantic_recall_enabled,
        "message": semantic_recall_message,
        "semantic_candidate_count": semantic_candidate_count,
        "suspicious_result_count": len(suspicious_results),
        "results": suspicious_results,
    }


def run_compare_pipeline(request: ComparePipelineRequest) -> dict[str, Any]:
    preset = get_detection_mode_preset(request.detection_mode)
    effective_merged_top_k = request.merged_top_k or preset.merged_top_k
    effective_compare_top_k = request.compare_top_k or preset.compare_top_k
    effective_top_k = request.top_k or preset.top_k
    effective_candidate_display_score_threshold = (
        float(request.candidate_display_score_threshold)
        if request.candidate_display_score_threshold is not None
        else 0.0
    )

    if not request.query_text.strip():
        raise ValueError("query_text is empty")
    if effective_compare_top_k <= 0:
        raise ValueError("compare_top_k must be > 0")
    if effective_top_k <= 0:
        raise ValueError("top_k must be > 0")
    if effective_merged_top_k <= 0:
        raise ValueError("merged_top_k must be > 0")
    if effective_candidate_display_score_threshold < 0 or effective_candidate_display_score_threshold > 1:
        raise ValueError("candidate_display_score_threshold must be between 0 and 1")
    if request.window_size <= 0:
        raise ValueError("window_size must be > 0")
    if request.step_size <= 0:
        raise ValueError("step_size must be > 0")

    lexical_payload = global_retrieve_candidates(
        db_path=request.db_path,
        query_text=request.query_text,
        candidate_limit_per_index=request.candidate_limit_per_index,
        merged_top_k=effective_merged_top_k,
        ngram_size=request.ngram_size,
        max_query_ngrams=request.max_query_ngrams,
        weight_coarse=request.weight_coarse,
        seed_term_count=request.seed_term_count,
    )

    semantic_payload: dict[str, object] = {
        "status": "disabled",
        "candidate_count": 0,
        "results": [],
        "error": "",
    }
    semantic_recall_status = "disabled"
    semantic_recall_message = "Semantic recall is currently disabled."
    semantic_recall_runtime_enabled = False

    if preset.semantic_recall_enabled and not request.disable_semantic_recall and request.semantic_config is not None:
        try:
            semantic_payload = semantic_retrieve_candidates(
                db_path=request.db_path,
                query_text=request.query_text,
                config=request.semantic_config,
            )
            semantic_recall_status = "semantic_ready"
            if semantic_payload.get("status") == "partial":
                semantic_recall_message = (
                    "Semantic recall is enabled with partial degradation. "
                    "Available recall sources were merged with lexical candidates."
                )
            else:
                semantic_recall_message = (
                    "Semantic recall is enabled and merged with lexical recall candidates."
                )
            semantic_recall_runtime_enabled = True
        except SemanticRetrievalError as exc:
            semantic_payload = {
                "status": "fallback_lexical_only",
                "candidate_count": 0,
                "results": [],
                "error": str(exc),
            }
            semantic_recall_status = "fallback_lexical_only"
            semantic_recall_message = "Semantic recall is unavailable. The pipeline automatically fell back to lexical recall only."

    merged_candidates = (
        merge_recall_candidates(
            lexical_results=list(lexical_payload["results"]),
            semantic_results=list(semantic_payload["results"]),
            merged_top_k=effective_merged_top_k,
        )
        if semantic_recall_runtime_enabled
        else list(lexical_payload["results"])
    )

    coarse_candidates = merged_candidates[:effective_compare_top_k]
    chapter_uids = [int(item["chapter_uid"]) for item in coarse_candidates]
    sliced_candidates = slice_candidate_chapters(
        db_path=request.db_path,
        chapter_uids=chapter_uids,
        window_size=request.window_size,
        step_size=request.step_size,
    )
    fine_results = compare_query_to_candidates(
        query_text=request.query_text,
        candidates=coarse_candidates,
        sliced_candidates=sliced_candidates,
        window_size=request.window_size,
        step_size=request.step_size,
        ngram_size=request.ngram_size,
    )
    reuse_detection = build_reuse_detection_section(fine_results=fine_results, top_k=effective_top_k)
    rewrite_detection = build_rewrite_detection_section(
        fine_results=fine_results,
        top_k=effective_top_k,
        semantic_recall_enabled=semantic_recall_runtime_enabled,
        semantic_recall_status=semantic_recall_status,
        semantic_recall_message=semantic_recall_message,
        semantic_candidate_count=int(semantic_payload["candidate_count"]),
    )
    top_results = list(reuse_detection["results"])
    review_rows = build_review_rows(
        query_text=request.query_text,
        fine_results=top_results,
        detection_mode=preset.name,
    )

    return {
        "query_text": request.query_text,
        "detection_mode": preset.name,
        "detection_mode_label": preset.label,
        "detection_mode_description": preset.description,
        "params": {
            "candidate_limit_per_index": request.candidate_limit_per_index,
            "merged_top_k": effective_merged_top_k,
            "compare_top_k": effective_compare_top_k,
            "top_k": effective_top_k,
            "candidate_display_score_threshold": round(effective_candidate_display_score_threshold, 4),
            "window_size": request.window_size,
            "step_size": request.step_size,
            "ngram_size": request.ngram_size,
            "max_query_ngrams": request.max_query_ngrams,
            "weight_coarse": request.weight_coarse,
            "seed_term_count": request.seed_term_count,
            "semantic_query_window_size": (
                request.semantic_config.query_window_size if request.semantic_config else None
            ),
            "semantic_query_overlap_size": (
                request.semantic_config.query_overlap_size if request.semantic_config else None
            ),
        },
        "capabilities": {
            "reuse_detection_enabled": True,
            "rewrite_detection_enabled": preset.rewrite_detection_enabled,
            "semantic_recall_enabled": semantic_recall_runtime_enabled,
        },
        "coarse": {
            "target_count": lexical_payload["target_count"],
            "candidate_limit_per_index": lexical_payload["candidate_limit_per_index"],
            "merged_top_k": lexical_payload["merged_top_k"],
            "index_summaries": [
                {
                    "dataset_key": result["dataset_key"],
                    "table_name": result["table_name"],
                    "candidate_count": result["payload"]["candidate_count"],
                }
                for result in lexical_payload["index_results"]
            ],
            "semantic": {
                "status": semantic_payload["status"],
                "candidate_count": semantic_payload["candidate_count"],
                "error": semantic_payload.get("error", ""),
                "results": list(semantic_payload["results"])[:effective_compare_top_k],
            },
            "merged_results": merged_candidates,
            "results": coarse_candidates,
        },
        "reuse_detection": reuse_detection,
        "rewrite_detection": (
            rewrite_detection
            if preset.rewrite_detection_enabled
            else {
                "enabled": False,
                "status": "disabled",
                "semantic_recall_enabled": False,
                "message": "Rewrite-detection output is disabled in reuse mode.",
                "semantic_candidate_count": 0,
                "suspicious_result_count": 0,
                "results": [],
            }
        ),
        "fine": {
            "candidate_count": len(coarse_candidates),
            "sliced_candidate_count": len(sliced_candidates),
            "compared_candidate_count": len(fine_results),
            "results": top_results,
            "review_rows": review_rows,
        },
    }
