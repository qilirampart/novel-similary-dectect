from __future__ import annotations

import pytest

from scripts.deploy_ecs_release_v1 import inspect_remote, validate_release_relative_dir


class FakeRemote:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> str:
        self.commands.append(command)
        if command.startswith("readlink"):
            return "/opt/novel-similarity-service/releases/current-test\n"
        return "ok\n"


def test_remote_inspection_never_requests_environment_values(capsys: object) -> None:
    remote = FakeRemote()

    inspect_remote(
        remote,
        remote_root="/opt/novel-similarity-service",
        service_name="novel-similarity-api.service",
    )

    assert all("Environment" not in command for command in remote.commands)


@pytest.mark.parametrize("value", ["", ".", "..", "../shared", "/tmp", "web/../../shared"])
def test_replace_directory_rejects_paths_outside_release(value: str) -> None:
    with pytest.raises(ValueError):
        validate_release_relative_dir(value)


def test_replace_directory_accepts_normalized_release_path() -> None:
    assert validate_release_relative_dir("web\\dist") == "web/dist"
