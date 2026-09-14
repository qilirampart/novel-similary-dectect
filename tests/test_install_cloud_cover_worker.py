from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from scripts import install_cloud_cover_worker_v1 as installer


def test_worker_unit_tracks_api_restarts_and_uses_shared_environment() -> None:
    unit = installer.render_worker_unit(
        remote_root="/opt/novel-similarity-service",
        api_service="novel-similarity-api.service",
    )

    assert "PartOf=novel-similarity-api.service" in unit
    assert "After=network-online.target novel-similarity-api.service mihomo-cover.service" in unit
    assert "EnvironmentFile=/opt/novel-similarity-service/shared/novel-similarity.env" in unit
    assert "WorkingDirectory=/opt/novel-similarity-service/current" in unit
    assert "ExecStart=/opt/novel-similarity-service/shared/venv/bin/python scripts/run_cover_worker_v1.py" in unit
    assert "Restart=always" in unit
    assert "api_key" not in unit.lower()
    assert "token" not in unit.lower()


@pytest.mark.parametrize(
    "remote_root",
    ["/", "/opt", "/srv/other-project", "/opt/novel-similarity-service/../other"],
)
def test_worker_installer_rejects_unsafe_remote_roots(remote_root: str) -> None:
    with pytest.raises(ValueError, match="remote root"):
        installer.validate_remote_root(remote_root)


def test_worker_installer_accepts_the_isolated_project_root() -> None:
    assert installer.validate_remote_root("/opt/novel-similarity-service") == PurePosixPath(
        "/opt/novel-similarity-service"
    )


def test_worker_preflight_reports_missing_required_environment_keys() -> None:
    missing = installer.missing_required_environment_keys(
        {
            "COVER_MONITOR_DB",
            "COVER_MONITOR_STORAGE_BACKEND",
            "COVER_MONITOR_VISION_API_BASE",
        }
    )

    assert "COVER_MONITOR_VISION_API_KEY" in missing
    assert "COVER_MONITOR_STAGING_ROOT" in missing
    assert "COVER_MONITOR_DB" not in missing


class FakeRemote:
    def __init__(self) -> None:
        self.installed_units: list[tuple[str, str]] = []
        self.commands: list[str] = []

    def inspect_worker_prerequisites(self, remote_root: PurePosixPath) -> dict[str, object]:
        return {
            "current_release": str(remote_root / "releases" / "test"),
            "worker_script_ready": True,
            "python_ready": True,
            "dependencies_ready": True,
            "missing_dependencies": [],
            "environment_file_ready": True,
            "environment_keys": sorted(installer.REQUIRED_ENVIRONMENT_KEYS),
            "proxy_service": "active",
        }

    def install_unit(self, service_name: str, content: str) -> None:
        self.installed_units.append((service_name, content))

    def run(self, command: str, **kwargs: object) -> str:
        self.commands.append(command)
        return "active"


def test_default_preflight_does_not_install_or_start_the_worker() -> None:
    remote = FakeRemote()

    report = installer.provision_worker(
        remote,
        remote_root="/opt/novel-similarity-service",
        service_name="novel-similarity-cover-worker.service",
        api_service="novel-similarity-api.service",
        execute=False,
    )

    assert report["mode"] == "preflight"
    assert remote.installed_units == []
    assert remote.commands == []


def test_execute_installs_and_starts_only_after_preflight_passes() -> None:
    remote = FakeRemote()

    report = installer.provision_worker(
        remote,
        remote_root="/opt/novel-similarity-service",
        service_name="novel-similarity-cover-worker.service",
        api_service="novel-similarity-api.service",
        execute=True,
    )

    assert report["mode"] == "execute"
    assert len(remote.installed_units) == 1
    assert remote.installed_units[0][0] == "novel-similarity-cover-worker.service"
    assert remote.commands == [
        "systemctl daemon-reload",
        "systemctl enable --now novel-similarity-cover-worker.service",
        "systemctl is-active novel-similarity-cover-worker.service",
    ]


def test_execute_is_blocked_when_cover_worker_dependencies_are_missing() -> None:
    remote = FakeRemote()
    original_inspect = remote.inspect_worker_prerequisites

    def missing_dependencies(remote_root: PurePosixPath) -> dict[str, object]:
        result = original_inspect(remote_root)
        result["dependencies_ready"] = False
        return result

    remote.inspect_worker_prerequisites = missing_dependencies  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="dependencies_ready"):
        installer.provision_worker(
            remote,
            remote_root="/opt/novel-similarity-service",
            service_name="novel-similarity-cover-worker.service",
            api_service="novel-similarity-api.service",
            execute=True,
        )

    assert remote.installed_units == []
