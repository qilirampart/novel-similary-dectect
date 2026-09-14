from __future__ import annotations

import pytest

from scripts.smoke_cloud_cover_candidate_v1 import build_smoke_command


def test_candidate_smoke_is_one_shot_and_does_not_dump_environment() -> None:
    command = build_smoke_command("release-1")

    assert "--once" in command
    assert "candidate-smoke" in command
    assert "readlink -f" in command
    assert "printenv" not in command
    assert " env " not in command


@pytest.mark.parametrize("release_name", ["../shared", "/tmp/release", "bad name"])
def test_candidate_smoke_rejects_unsafe_release_names(release_name: str) -> None:
    with pytest.raises(ValueError):
        build_smoke_command(release_name)
