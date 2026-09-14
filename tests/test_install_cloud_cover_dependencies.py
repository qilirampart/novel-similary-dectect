from __future__ import annotations

from scripts.install_cloud_cover_dependencies_v1 import PACKAGES, build_install_command


def test_dependency_installer_defaults_to_dry_run_and_project_proxy() -> None:
    command = build_install_command(execute=False)

    assert "--dry-run" in command
    assert "127.0.0.1:17890" in command
    assert "https://pypi.org/simple" in command
    assert all(package in command for package in PACKAGES)


def test_dependency_installer_requires_execute_to_remove_dry_run() -> None:
    assert "--dry-run" not in build_install_command(execute=True)
