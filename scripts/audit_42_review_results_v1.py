from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "docs/42_coverage_rerun_20260902/task_b3b87390-1d43-4487-b3a3-225c1f0dbc74_raw.json"
OUT = ROOT / "docs/42_coverage_rerun_20260902/42条待复核内容审计.json"


def metric(candidate: dict[str, Any], name: str) -> Any:
    metrics = candidate.get("match_metrics")
    return metrics.get(name) if isinstance(metrics, dict) else None


def evidence(candidate: dict[str, Any]) -> str:
    value = candidate.get("evidence")
    if not isinstance(value, dict):
        return ""
    return str(value.get("window_text") or value.get("window_text_preview") or "")


def candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "book_id": candidate.get("book_id", ""),
        "book_name": candidate.get("book_name", ""),
        "episode_order": candidate.get("episode_order"),
        "language_code": candidate.get("language_code", ""),
        "semantic_score": candidate.get("semantic_score"),
        "retrieval_sources": candidate.get("retrieval_sources", []),
        "shared_trigram_count": metric(candidate, "shared_trigram_count"),
        "query_coverage_rate": metric(candidate, "query_coverage_rate"),
        "evidence_coverage_rate": metric(candidate, "evidence_coverage_rate"),
        "retrieved_window_query_coverage_rate": metric(candidate, "retrieved_window_query_coverage_rate"),
        "shared_trigrams": metric(candidate, "shared_trigrams") or [],
        "evidence_text": evidence(candidate),
    }


def main() -> None:
    data = json.loads(RAW.read_text(encoding="utf-8"))
    audits: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    for item in data.get("items", []):
        payload = json.loads(item.get("result_payload_json") or "{}")
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        if decision.get("status") != "review_required":
            continue
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        reason = str(decision.get("reason") or "")
        outcome = str(decision.get("outcome") or "")
        reason_counts[reason] += 1
        outcome_counts[outcome] += 1
        fallback_attempts = payload.get("translation_fallback", {}).get("attempts", [])
        attempt_summary = []
        if isinstance(fallback_attempts, list):
            for attempt in fallback_attempts:
                if not isinstance(attempt, dict):
                    continue
                attempt_decision = attempt.get("decision") if isinstance(attempt.get("decision"), dict) else {}
                attempt_summary.append({
                    "target_language_code": attempt.get("target_language_code", ""),
                    "provider": attempt.get("provider", ""),
                    "duration_seconds": attempt.get("duration_seconds"),
                    "outcome": attempt.get("outcome", ""),
                    "decision_reason": attempt_decision.get("reason", ""),
                    "content_match_status": attempt_decision.get("content_match_status", ""),
                    "candidate_book_id": attempt_decision.get("book_id", ""),
                    "candidate_book_name": attempt_decision.get("book_name", ""),
                    "semantic_score": attempt_decision.get("semantic_score"),
                    "ordered_alignment_score": attempt_decision.get("ordered_alignment_score"),
                    "aligned_character_count": attempt_decision.get("aligned_character_count"),
                    "shared_match_count": attempt_decision.get("shared_match_count"),
                    "candidate_evidence": (
                        ((attempt.get("candidates") or [{}])[0].get("evidence_text", ""))
                        if isinstance(attempt.get("candidates"), list) and attempt.get("candidates") and isinstance((attempt.get("candidates") or [{}])[0], dict)
                        else ""
                    ),
                })
        audits.append({
            "item_order": item.get("item_order"),
            "source_ref": item.get("source_ref", ""),
            "query_language_code": payload.get("query_language_code", ""),
            "query_text": payload.get("query_text", ""),
            "translated_query_text": payload.get("translated_query_text", ""),
            "duration_seconds": item.get("duration_seconds"),
            "outcome": outcome,
            "reason": reason,
            "content_match_status": decision.get("content_match_status", ""),
            "title_resolution": decision.get("title_resolution", ""),
            "user_message": decision.get("user_message", ""),
            "review_feedback": decision.get("review_feedback", {}),
            "translation_attempts": attempt_summary,
            "decision_candidate": {
                "book_id": decision.get("book_id") or decision.get("matched_book_id", ""),
                "book_name": decision.get("book_name") or decision.get("matched_book_name", ""),
                "semantic_score": decision.get("semantic_score"),
                "ordered_alignment_score": decision.get("ordered_alignment_score"),
                "aligned_character_count": decision.get("aligned_character_count"),
                "evidence_coverage_rate": decision.get("evidence_coverage_rate"),
                "shared_match_count": decision.get("shared_match_count"),
            },
            "translation_target_language_code": payload.get("translation_target_language_code", ""),
            "top_candidates": [candidate_summary(candidate) for candidate in candidates[:3] if isinstance(candidate, dict)],
            "selected_candidate_evidence": next(
                (
                    candidate_summary(candidate)
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and str(candidate.get("book_id") or "") == str(decision.get("book_id") or "")
                ),
                {},
            ),
            "content_candidate_options": payload.get("content_candidate_options", []),
        })
    result = {
        "review_count": len(audits),
        "reason_counts": dict(reason_counts),
        "outcome_counts": dict(outcome_counts),
        "items": audits,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"review_count": len(audits), "reason_counts": dict(reason_counts), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
