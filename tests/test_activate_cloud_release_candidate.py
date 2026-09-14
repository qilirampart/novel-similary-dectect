from __future__ import annotations

import pytest

from scripts.activate_cloud_release_candidate_v1 import activate_candidate


class FakeRemote:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> str:
        self.commands.append(command)
        if command.startswith("readlink"):
            return "/opt/novel-similarity-service/releases/current-release\n"
        if command.startswith("systemctl is-active"):
            return "active\n"
        return ""


def test_activation_defaults_to_read_only_preflight() -> None:
    remote = FakeRemote()

    report = activate_candidate(remote, release_name="candidate-1", execute=False)

    assert report["activated"] is False
    assert not any("ln -sfn" in command for command in remote.commands)
    assert not any("systemctl restart" in command for command in remote.commands)
    assert not any(".backup" in command for command in remote.commands)


@pytest.mark.parametrize("release_name", ["../shared", "/tmp/release", "bad name"])
def test_activation_rejects_unsafe_release_name(release_name: str) -> None:
    with pytest.raises(ValueError):
        activate_candidate(FakeRemote(), release_name=release_name, execute=False)
