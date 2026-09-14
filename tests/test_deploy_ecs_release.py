from __future__ import annotations

from scripts.deploy_ecs_release_v1 import inspect_remote


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
