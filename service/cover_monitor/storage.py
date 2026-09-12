from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
from typing import ContextManager, Iterator, Literal, Protocol
from uuid import uuid4


class CoverAssetStorageError(RuntimeError):
    pass


class InvalidCoverStorageKey(CoverAssetStorageError):
    pass


class CoverAssetNotFoundError(CoverAssetStorageError):
    pass


class CoverAssetConflictError(CoverAssetStorageError):
    pass


@dataclass(frozen=True)
class CoverAssetDelivery:
    kind: Literal["file", "redirect"]
    location: str


class CoverAssetStorage(Protocol):
    def put_bytes(self, storage_key: str, content: bytes) -> Path:
        """Persist immutable content and return a local path usable by the current worker."""

    def materialize(self, storage_key: str) -> ContextManager[Path]:
        """Provide a temporary local path for image decoders and vision clients."""

    def delivery(self, storage_key: str, *, expires_seconds: int) -> CoverAssetDelivery:
        """Return an authenticated API delivery target for a stored asset."""


class LocalCoverAssetStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, storage_key: str, content: bytes) -> Path:
        if not isinstance(content, bytes) or not content:
            raise ValueError("封面内容必须是非空 bytes")
        target = self.resolve_local_path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            self._assert_same_content(target, content)
            return target

        temporary = target.with_suffix(f"{target.suffix}.part-{uuid4().hex}")
        try:
            temporary.write_bytes(content)
            try:
                os.link(temporary, target)
            except FileExistsError:
                self._assert_same_content(target, content)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @contextmanager
    def materialize(self, storage_key: str) -> Iterator[Path]:
        target = self.resolve_local_path(storage_key)
        if not target.is_file():
            raise CoverAssetNotFoundError("封面证据文件缺失")
        yield target

    def delivery(self, storage_key: str, *, expires_seconds: int) -> CoverAssetDelivery:
        if int(expires_seconds) <= 0:
            raise ValueError("交付地址有效期必须大于 0")
        target = self.resolve_local_path(storage_key)
        if not target.is_file():
            raise CoverAssetNotFoundError("封面证据文件缺失")
        return CoverAssetDelivery(kind="file", location=str(target))

    def resolve_local_path(self, storage_key: str) -> Path:
        key = self._validate_key(storage_key)
        target = self.root.joinpath(*key.parts).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise InvalidCoverStorageKey("封面存储键越出资产目录") from exc
        return target

    @staticmethod
    def _validate_key(storage_key: str) -> PurePosixPath:
        raw = str(storage_key or "")
        if not raw or len(raw) > 1024 or "\\" in raw or any(ord(char) < 32 for char in raw):
            raise InvalidCoverStorageKey("非法封面存储键")
        key = PurePosixPath(raw)
        if key.is_absolute() or any(part in {"", ".", ".."} for part in key.parts):
            raise InvalidCoverStorageKey("非法封面存储键")
        return key

    @staticmethod
    def _assert_same_content(target: Path, content: bytes) -> None:
        if not target.is_file() or target.read_bytes() != content:
            raise CoverAssetConflictError("同一封面存储键已存在不同内容")
