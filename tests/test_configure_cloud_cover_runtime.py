from __future__ import annotations

from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import configure_cloud_cover_runtime_v1 as runtime_config


def _inputs() -> runtime_config.CoverRuntimeInputs:
    return runtime_config.CoverRuntimeInputs(
        api_base="https://vision.example.com/v1",
        api_key="super-secret-api-key",
        model="qwen3-vl-flash",
        proxy_url="http://127.0.0.1:17890",
        cookie_bytes=b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tsecret\n",
    )


def test_script_can_start_directly_from_the_project_root() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "configure_cloud_cover_runtime_v1.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_runtime_environment_uses_isolated_shared_paths() -> None:
    values = runtime_config.build_environment_values(
        _inputs(),
        remote_root="/opt/novel-similarity-service",
    )

    prefix = "/opt/novel-similarity-service/shared/runtime/cover_monitor"
    assert values["COVER_MONITOR_DB"] == f"{prefix}/cover_monitor_v1.sqlite3"
    assert values["COVER_MONITOR_ASSET_ROOT"] == f"{prefix}/assets"
    assert values["COVER_MONITOR_STAGING_ROOT"] == f"{prefix}/staging"
    assert values["COVER_MONITOR_IMPORT_ROOT"] == f"{prefix}/imports"
    assert values["COVER_MONITOR_EXPORT_ROOT"] == f"{prefix}/exports"
    assert values["COVER_MONITOR_COOKIE_PATH"].endswith("/shared/secrets/youtube_cookies.txt")
    assert values["COVER_MONITOR_STORAGE_BACKEND"] == "local"


def test_runtime_inputs_reject_nonlocal_proxy_and_insecure_model_endpoint() -> None:
    with pytest.raises(ValueError, match="proxy"):
        runtime_config.validate_runtime_inputs(
            runtime_config.CoverRuntimeInputs(
                **{**_inputs().__dict__, "proxy_url": "http://10.0.0.8:17890"}
            )
        )
    with pytest.raises(ValueError, match="HTTPS"):
        runtime_config.validate_runtime_inputs(
            runtime_config.CoverRuntimeInputs(
                **{**_inputs().__dict__, "api_base": "http://vision.example.com/v1"}
            )
        )


def test_environment_merge_preserves_other_services_and_replaces_cover_keys() -> None:
    existing = (
        "NOVEL_SIMILARITY_DB=/shared/novel.sqlite3\n"
        "COVER_MONITOR_DB=/old/cover.sqlite3\n"
        "# existing comment\n"
    ).encode("utf-8")

    merged = runtime_config.merge_environment_file(
        existing,
        {"COVER_MONITOR_DB": "/new/cover.sqlite3", "COVER_MONITOR_VISION_MODEL": "model"},
    ).decode("utf-8")

    assert "NOVEL_SIMILARITY_DB=/shared/novel.sqlite3" in merged
    assert "# existing comment" in merged
    assert "/old/cover.sqlite3" not in merged
    assert merged.count("COVER_MONITOR_DB=") == 1
    assert 'COVER_MONITOR_DB="/new/cover.sqlite3"' in merged


def test_environment_merge_is_idempotent_for_managed_comment() -> None:
    updates = {"COVER_MONITOR_DB": "/new/cover.sqlite3"}

    first = runtime_config.merge_environment_file(b"BASE=value\n", updates)
    second = runtime_config.merge_environment_file(first, updates).decode("utf-8")

    assert second.count("# Cover monitor runtime (managed by project tooling)") == 1


def test_environment_values_reject_line_injection() -> None:
    with pytest.raises(ValueError, match="line break"):
        runtime_config.merge_environment_file(
            b"BASE=value\n",
            {"COVER_MONITOR_VISION_MODEL": "safe\nINJECTED=value"},
        )


@pytest.mark.parametrize(
    "api_base",
    [
        "https://user:password@vision.example.com/v1",
        "https://vision.example.com/v1?token=secret",
        "https://vision.example.com/v1#secret",
    ],
)
def test_runtime_inputs_reject_model_urls_that_can_embed_secrets(api_base: str) -> None:
    with pytest.raises(ValueError, match="credentials or query"):
        runtime_config.validate_runtime_inputs(
            runtime_config.CoverRuntimeInputs(
                **{**_inputs().__dict__, "api_base": api_base}
            )
        )


class FakeRemote:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []

    def read_optional(self, path: PurePosixPath) -> bytes | None:
        return b"NOVEL_SIMILARITY_DB=/shared/novel.sqlite3\n"

    def apply_runtime_config(self, **kwargs: object) -> None:
        self.applied.append(kwargs)


def test_default_mode_is_read_only_and_report_is_secret_free() -> None:
    remote = FakeRemote()

    report = runtime_config.configure_runtime(
        remote,
        _inputs(),
        remote_root="/opt/novel-similarity-service",
        execute=False,
    )

    assert report["mode"] == "preflight"
    assert remote.applied == []
    rendered = repr(report)
    assert "super-secret-api-key" not in rendered
    assert "sid\\tsecret" not in rendered
    assert report["model"] == "qwen3-vl-flash"
    assert report["api_host"] == "vision.example.com"


def test_execute_writes_environment_and_cookie_without_reporting_secrets() -> None:
    remote = FakeRemote()

    report = runtime_config.configure_runtime(
        remote,
        _inputs(),
        remote_root="/opt/novel-similarity-service",
        execute=True,
    )

    assert report["configured"] is True
    assert len(remote.applied) == 1
    applied = remote.applied[0]
    assert b"super-secret-api-key" in applied["environment_bytes"]
    assert applied["cookie_bytes"] == _inputs().cookie_bytes
    assert "super-secret-api-key" not in repr(report)


def test_load_inputs_selects_configured_profile_without_exposing_it(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "api_config.json").write_text(
        '{"llm":{"profiles":[{"id":"p1","enabled":true,'
        '"api_base":"https://vision.example.com/v1","api_key":"key","model":"vision"}]},'
        '"cover_review":{"profile_id":"p1"}}',
        encoding="utf-8",
    )
    (runtime / "youtube_proxy.txt").write_text("http://127.0.0.1:7897", encoding="utf-8")
    (runtime / "youtube_cookies.txt").write_bytes(b"cookie-data")

    loaded = runtime_config.load_runtime_inputs(runtime)

    assert loaded.api_key == "key"
    assert loaded.model == "vision"
    assert loaded.proxy_url == "http://127.0.0.1:17890"
    assert loaded.cookie_bytes == b"cookie-data"


def test_remote_reader_rejects_a_symbolic_link() -> None:
    class SymlinkSftp:
        def lstat(self, path: str) -> object:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)

        def file(self, path: str, mode: str) -> object:
            pytest.fail("symbolic link must not be opened")

    remote = runtime_config.RemoteSession.__new__(runtime_config.RemoteSession)
    remote._sftp = SymlinkSftp()

    with pytest.raises(RuntimeError, match="non-regular remote file"):
        remote.read_optional(PurePosixPath("/opt/novel-similarity-service/shared/env"))


def test_remote_directory_creation_rejects_a_symbolic_link_in_the_path() -> None:
    class SymlinkDirectorySftp:
        def lstat(self, path: str) -> object:
            mode = stat.S_IFLNK | 0o777 if path.endswith("/shared") else stat.S_IFDIR | 0o700
            return SimpleNamespace(st_mode=mode)

        def mkdir(self, path: str, mode: int) -> None:
            pytest.fail("existing path must not be recreated")

        def chmod(self, path: str, mode: int) -> None:
            pytest.fail("unsafe directory must not be chmodded")

    remote = runtime_config.RemoteSession.__new__(runtime_config.RemoteSession)
    remote._sftp = SymlinkDirectorySftp()

    with pytest.raises(RuntimeError, match="unsafe remote directory"):
        remote._mkdir_tree(
            PurePosixPath("/opt/novel-similarity-service/shared/secrets"),
            mode=0o700,
        )
