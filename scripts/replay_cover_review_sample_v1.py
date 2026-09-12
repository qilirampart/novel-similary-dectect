from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from service.cover_monitor.reviewer import CoverVisionProfile, CoverVisionReviewer


def _vision_profile(config_path: Path) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    profiles = payload.get("llm", {}).get("profiles", [])
    cover = payload.get("cover_review") or payload.get("video_review") or {}
    profile_id = str(cover.get("profile_id") or "").strip()
    ready = [
        item
        for item in profiles
        if isinstance(item, dict)
        and item.get("enabled", True)
        and item.get("api_base")
        and item.get("api_key")
        and item.get("model")
    ]
    selected = next((item for item in ready if str(item.get("id")) == profile_id), None)
    if selected is None and ready:
        selected = ready[0]
    if selected is None:
        raise ValueError("没有可用的视觉模型配置")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a saved cover review sample")
    parser.add_argument("--source-json", required=True)
    parser.add_argument("--assistant-runtime", required=True)
    args = parser.parse_args()

    source_path = Path(args.source_json).resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    profile = _vision_profile(Path(args.assistant_runtime).resolve() / "api_config.json")
    reviewer = CoverVisionReviewer(
        CoverVisionProfile(
            api_base=str(profile["api_base"]),
            api_key=str(profile["api_key"]),
            model=str(profile["model"]),
            timeout_seconds=90,
        )
    )

    results: list[dict[str, Any]] = []
    for source_item in source.get("items", []):
        video = source_item.get("video") or {}
        asset = source_item.get("asset") or {}
        outcome = reviewer.review(
            image_path=str(asset.get("local_path") or ""),
            video_title=str(video.get("title") or ""),
            intensity="standard",
        )
        result = outcome.result.to_dict() if outcome.result is not None else {}
        result.pop("raw_response", None)
        item = {
            "index": source_item.get("index"),
            "video_id": video.get("video_id"),
            "title": video.get("title"),
            "status": outcome.status,
            "duration_seconds": round(outcome.duration_seconds, 3),
            "detection": result,
            "error_type": outcome.error_type,
            "error_message": outcome.error_message,
        }
        results.append(item)
        print(
            json.dumps(
                {
                    "index": item["index"],
                    "video_id": item["video_id"],
                    "status": item["status"],
                    "risk": result.get("overall_risk"),
                    "seconds": item["duration_seconds"],
                },
                ensure_ascii=True,
            ),
            flush=True,
        )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = source_path.with_name(f"封面真实样本提示词重放_{timestamp}.json")
    payload = {
        "replayed_at": datetime.now().isoformat(timespec="seconds"),
        "source_json": str(source_path),
        "model": profile["model"],
        "items": results,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(target)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
