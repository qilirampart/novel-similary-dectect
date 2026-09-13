from __future__ import annotations

from pathlib import Path

from api.config import ApiSettings
from service.cover_monitor.storage import LocalCoverAssetStorage, OssCoverAssetStorage


def test_cover_storage_settings_default_to_local_without_loading_oss(tmp_path: Path) -> None:
    settings = ApiSettings(
        cover_monitor_storage_backend="local",
        cover_monitor_asset_root=str(tmp_path / "assets"),
        cover_monitor_staging_root=str(tmp_path / "staging"),
    )

    storage = settings.build_cover_asset_storage()

    assert isinstance(storage, LocalCoverAssetStorage)


def test_cover_storage_settings_build_oss_with_injected_client(tmp_path: Path) -> None:
    settings = ApiSettings(
        cover_monitor_storage_backend="oss",
        cover_monitor_staging_root=str(tmp_path / "staging"),
        cover_monitor_oss_region="cn-hangzhou",
        cover_monitor_oss_endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com",
        cover_monitor_oss_bucket="private-covers",
        cover_monitor_oss_prefix="cover-monitor/v1",
    )

    storage = settings.build_cover_asset_storage(oss_client=object())

    assert isinstance(storage, OssCoverAssetStorage)


def test_oss_runtime_dirs_create_staging_without_creating_local_asset_root(tmp_path: Path) -> None:
    settings = ApiSettings(
        task_upload_root=str(tmp_path / "task-uploads"),
        task_export_root=str(tmp_path / "task-exports"),
        cover_monitor_storage_backend="oss",
        cover_monitor_asset_root=str(tmp_path / "should-not-exist"),
        cover_monitor_staging_root=str(tmp_path / "staging"),
        cover_monitor_import_root=str(tmp_path / "imports"),
    )

    settings.ensure_runtime_dirs()

    assert (tmp_path / "staging").is_dir()
    assert not (tmp_path / "should-not-exist").exists()
