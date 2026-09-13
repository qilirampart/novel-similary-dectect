from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from service.cover_monitor.storage import (
    CoverAssetConflictError,
    InvalidCoverStorageKey,
    LocalCoverAssetStorage,
    OssCoverAssetStorage,
    build_cover_asset_storage,
)


def test_local_storage_writes_and_materializes_an_immutable_asset(tmp_path: Path) -> None:
    storage = LocalCoverAssetStorage(tmp_path / "assets")
    key = "video001/abc123.jpg"

    stored_path = storage.put_bytes(key, b"first-cover")
    repeated_path = storage.put_bytes(key, b"first-cover")

    assert stored_path == repeated_path
    assert stored_path.read_bytes() == b"first-cover"
    with storage.materialize(key) as materialized:
        assert materialized == stored_path
        assert materialized.read_bytes() == b"first-cover"
    assert not list((tmp_path / "assets").rglob("*.part-*"))


def test_local_storage_rejects_overwriting_an_existing_key_with_different_content(tmp_path: Path) -> None:
    storage = LocalCoverAssetStorage(tmp_path / "assets")
    storage.put_bytes("video001/abc123.jpg", b"first-cover")

    with pytest.raises(CoverAssetConflictError):
        storage.put_bytes("video001/abc123.jpg", b"different-cover")

    assert storage.resolve_local_path("video001/abc123.jpg").read_bytes() == b"first-cover"


@pytest.mark.parametrize(
    "key",
    ["", "/absolute.jpg", "../escape.jpg", "video/../../escape.jpg", "video\\escape.jpg"],
)
def test_local_storage_rejects_unsafe_object_keys(tmp_path: Path, key: str) -> None:
    storage = LocalCoverAssetStorage(tmp_path / "assets")

    with pytest.raises(InvalidCoverStorageKey):
        storage.resolve_local_path(key)


def test_local_storage_delivery_is_a_checked_existing_file(tmp_path: Path) -> None:
    storage = LocalCoverAssetStorage(tmp_path / "assets")
    path = storage.put_bytes("video001/abc123.jpg", b"cover")

    delivery = storage.delivery("video001/abc123.jpg", expires_seconds=300)

    assert delivery.kind == "file"
    assert delivery.location == str(path)


class FakeOssClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, str]] = []

    def object_exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    def put_object(self, bucket: str, key: str, content: bytes) -> None:
        self.put_calls.append((bucket, key))
        self.objects[(bucket, key)] = content

    def download_object(self, bucket: str, key: str, target: Path) -> None:
        self.download_calls.append((bucket, key))
        target.write_bytes(self.objects[(bucket, key)])


def _content_addressed_key(content: bytes) -> str:
    return f"video001/{sha256(content).hexdigest()}.jpg"


def test_oss_storage_uploads_once_and_delivers_through_local_proxy_file(tmp_path: Path) -> None:
    client = FakeOssClient()
    storage = OssCoverAssetStorage(
        client=client,
        bucket="private-covers",
        prefix="cover-monitor/v1",
        staging_root=tmp_path / "staging",
    )
    content = b"cover-image-content"
    key = _content_addressed_key(content)

    first = storage.put_bytes(key, content)
    second = storage.put_bytes(key, content)
    delivery = storage.delivery(key, expires_seconds=300)

    assert first == second and first.read_bytes() == content
    assert client.put_calls == [("private-covers", f"cover-monitor/v1/{key}")]
    assert delivery.kind == "file"
    assert Path(delivery.location).read_bytes() == content


def test_oss_storage_delivery_restores_and_verifies_missing_staging_file(tmp_path: Path) -> None:
    client = FakeOssClient()
    content = b"remote-evidence-content"
    key = _content_addressed_key(content)
    object_key = f"cover-monitor/v1/{key}"
    client.objects[("private-covers", object_key)] = content
    storage = OssCoverAssetStorage(
        client=client,
        bucket="private-covers",
        prefix="cover-monitor/v1",
        staging_root=tmp_path / "staging",
    )

    delivery = storage.delivery(key, expires_seconds=300)

    assert delivery.kind == "file"
    assert Path(delivery.location).read_bytes() == content
    assert client.download_calls == [("private-covers", object_key)]


def test_oss_storage_replaces_a_corrupt_staging_file_from_oss(tmp_path: Path) -> None:
    client = FakeOssClient()
    content = b"trusted-remote-evidence"
    key = _content_addressed_key(content)
    object_key = f"cover-monitor/v1/{key}"
    client.objects[("private-covers", object_key)] = content
    storage = OssCoverAssetStorage(
        client=client,
        bucket="private-covers",
        prefix="cover-monitor/v1",
        staging_root=tmp_path / "staging",
    )
    corrupt = storage.staging.resolve_local_path(key)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"corrupt")

    with storage.materialize(key) as path:
        assert path.read_bytes() == content

    assert client.download_calls == [("private-covers", object_key)]


def test_oss_storage_downloads_and_verifies_a_missing_local_cache(tmp_path: Path) -> None:
    client = FakeOssClient()
    content = b"remote-cover-content"
    key = _content_addressed_key(content)
    object_key = f"cover-monitor/v1/{key}"
    client.objects[("private-covers", object_key)] = content
    storage = OssCoverAssetStorage(
        client=client,
        bucket="private-covers",
        prefix="cover-monitor/v1",
        staging_root=tmp_path / "staging",
    )

    with storage.materialize(key) as path:
        assert path.read_bytes() == content

    assert client.download_calls == [("private-covers", object_key)]


def test_oss_storage_rejects_content_that_does_not_match_hash_key(tmp_path: Path) -> None:
    storage = OssCoverAssetStorage(
        client=FakeOssClient(),
        bucket="private-covers",
        prefix="cover-monitor/v1",
        staging_root=tmp_path / "staging",
    )

    with pytest.raises(CoverAssetConflictError):
        storage.put_bytes("video001/" + "a" * 64 + ".jpg", b"different-content")


@pytest.mark.parametrize("bucket,prefix", [("", "cover-monitor/v1"), ("Bad_Bucket", "cover-monitor/v1"), ("ok-bucket", "../escape")])
def test_oss_storage_validates_bucket_and_prefix(tmp_path: Path, bucket: str, prefix: str) -> None:
    with pytest.raises(ValueError):
        OssCoverAssetStorage(
            client=FakeOssClient(),
            bucket=bucket,
            prefix=prefix,
            staging_root=tmp_path / "staging",
        )


def test_storage_factory_keeps_local_as_the_safe_default(tmp_path: Path) -> None:
    storage = build_cover_asset_storage(
        backend="local",
        local_root=tmp_path / "assets",
        staging_root=tmp_path / "staging",
    )

    assert isinstance(storage, LocalCoverAssetStorage)
    assert storage.root == (tmp_path / "assets").resolve()


def test_storage_factory_requires_complete_oss_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="OSS region"):
        build_cover_asset_storage(
            backend="oss",
            local_root=tmp_path / "assets",
            staging_root=tmp_path / "staging",
            oss_bucket="private-covers",
            oss_client=FakeOssClient(),
        )


def test_storage_factory_builds_oss_with_an_injected_client(tmp_path: Path) -> None:
    storage = build_cover_asset_storage(
        backend="oss",
        local_root=tmp_path / "assets",
        staging_root=tmp_path / "staging",
        oss_region="cn-hangzhou",
        oss_endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com",
        oss_bucket="private-covers",
        oss_prefix="cover-monitor/v1",
        oss_client=FakeOssClient(),
    )

    assert isinstance(storage, OssCoverAssetStorage)
