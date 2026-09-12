from __future__ import annotations

from pathlib import Path

import pytest

from service.cover_monitor.storage import (
    CoverAssetConflictError,
    InvalidCoverStorageKey,
    LocalCoverAssetStorage,
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
