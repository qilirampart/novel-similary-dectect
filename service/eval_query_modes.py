from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvalQueryPreset:
    name: str
    label: str
    description: str
    min_chars: int
    query_len: int
    skip_prefix: int


EVAL_QUERY_PRESETS: dict[str, EvalQueryPreset] = {
    "reuse": EvalQueryPreset(
        name="reuse",
        label="复用评估",
        description="更短的取样片段，适合词面复用和近似复用能力评估。",
        min_chars=600,
        query_len=120,
        skip_prefix=60,
    ),
    "semantic": EvalQueryPreset(
        name="semantic",
        label="语义评估",
        description="更长的取样片段，适合语义召回和洗稿检测能力评估。",
        min_chars=600,
        query_len=300,
        skip_prefix=60,
    ),
}


def list_eval_query_modes() -> list[str]:
    return list(EVAL_QUERY_PRESETS.keys())


def get_eval_query_preset(mode_name: str) -> EvalQueryPreset:
    try:
        return EVAL_QUERY_PRESETS[mode_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported eval query mode: {mode_name!r}") from exc


def dataset_eval_prefix(dataset_key: str) -> str:
    mapping = {
        "self_short_novels": "self_short",
        "online_owned_novels": "online_owned",
    }
    if dataset_key in mapping:
        return mapping[dataset_key]
    return dataset_key.replace("/", "_").replace("\\", "_").replace(" ", "_")


def default_eval_queries_out_path(
    dataset_key: str,
    eval_mode: str,
    query_len: int,
) -> str:
    prefix = dataset_eval_prefix(dataset_key)
    if eval_mode == "reuse" and query_len == 120:
        filename = f"{prefix}_eval_queries_v1.csv"
    elif eval_mode == "semantic":
        filename = f"{prefix}_eval_queries_semantic_q{query_len}_v1.csv"
    else:
        filename = f"{prefix}_eval_queries_{eval_mode}_q{query_len}_v1.csv"
    return str(Path("data_samples") / "retrieval_eval" / filename)
