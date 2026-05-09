from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectionModePreset:
    name: str
    label: str
    description: str
    merged_top_k: int
    compare_top_k: int
    top_k: int
    rewrite_detection_enabled: bool
    semantic_recall_enabled: bool


MODE_PRESETS: dict[str, DetectionModePreset] = {
    "reuse": DetectionModePreset(
        name="reuse",
        label="Reuse Detection",
        description="Focus on direct reuse, near-duplicate reuse, and strong text evidence.",
        merged_top_k=20,
        compare_top_k=10,
        top_k=5,
        rewrite_detection_enabled=False,
        semantic_recall_enabled=False,
    ),
    "rewrite": DetectionModePreset(
        name="rewrite",
        label="Reuse + Rewrite Detection",
        description="Keep reuse detection and reserve the rewrite-detection output structure for semantic recall integration.",
        merged_top_k=40,
        compare_top_k=20,
        top_k=10,
        rewrite_detection_enabled=True,
        semantic_recall_enabled=True,
    ),
}


def list_detection_modes() -> list[str]:
    return list(MODE_PRESETS.keys())


def get_detection_mode_preset(mode_name: str) -> DetectionModePreset:
    try:
        return MODE_PRESETS[mode_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported detection mode: {mode_name!r}") from exc
