from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
from typing import ContextManager, Iterator, Literal, Protocol
from urllib.parse import urlsplit
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


class OssObjectClient(Protocol):
    def object_exists(self, bucket: str, key: str) -> bool:
        ...

    def put_object(self, bucket: str, key: str, content: bytes) -> None:
        ...

    def download_object(self, bucket: str, key: str, target: Path) -> None:
        ...


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


class OssCoverAssetStorage:
    _BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    _CONTENT_HASH = re.compile(r"^[a-f0-9]{64}$")

    def __init__(
        self,
        *,
        client: OssObjectClient,
        bucket: str,
        prefix: str,
        staging_root: str | Path,
    ) -> None:
        self.client = client
        self.bucket = str(bucket or "").strip()
        if not self._BUCKET_NAME.fullmatch(self.bucket):
            raise ValueError("OSS Bucket 名称非法")
        prefix_text = str(prefix or "").strip().strip("/")
        try:
            self.prefix = LocalCoverAssetStorage._validate_key(prefix_text).as_posix()
        except InvalidCoverStorageKey as exc:
            raise ValueError("OSS 对象前缀非法") from exc
        self.staging = LocalCoverAssetStorage(staging_root)

    def put_bytes(self, storage_key: str, content: bytes) -> Path:
        self._validate_content_address(storage_key, content)
        local_path = self.staging.put_bytes(storage_key, content)
        object_key = self._object_key(storage_key)
        try:
            if not self.client.object_exists(self.bucket, object_key):
                self.client.put_object(self.bucket, object_key, content)
        except Exception as exc:
            raise CoverAssetStorageError("OSS 封面上传失败") from exc
        return local_path

    @contextmanager
    def materialize(self, storage_key: str) -> Iterator[Path]:
        yield self._ensure_materialized(storage_key)

    def delivery(self, storage_key: str, *, expires_seconds: int) -> CoverAssetDelivery:
        if int(expires_seconds) <= 0:
            raise ValueError("交付有效期必须大于 0")
        target = self._ensure_materialized(storage_key)
        return CoverAssetDelivery(kind="file", location=str(target))

    def _ensure_materialized(self, storage_key: str) -> Path:
        target = self.staging.resolve_local_path(storage_key)
        if target.is_file():
            try:
                self._validate_content_address(storage_key, target.read_bytes())
            except (CoverAssetConflictError, ValueError):
                target.unlink(missing_ok=True)
        if not target.is_file():
            object_key = self._object_key(storage_key)
            temporary = target.with_suffix(f"{target.suffix}.part-{uuid4().hex}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if not self.client.object_exists(self.bucket, object_key):
                    raise CoverAssetNotFoundError("OSS 封面对象不存在")
                self.client.download_object(self.bucket, object_key, temporary)
                content = temporary.read_bytes()
                self._validate_content_address(storage_key, content)
                target = self.staging.put_bytes(storage_key, content)
            except CoverAssetStorageError:
                raise
            except Exception as exc:
                raise CoverAssetStorageError("OSS 封面下载失败") from exc
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def _object_key(self, storage_key: str) -> str:
        key = LocalCoverAssetStorage._validate_key(storage_key).as_posix()
        return f"{self.prefix}/{key}"

    def _validate_content_address(self, storage_key: str, content: bytes) -> None:
        if not isinstance(content, bytes) or not content:
            raise ValueError("封面内容必须是非空 bytes")
        key = LocalCoverAssetStorage._validate_key(storage_key)
        claimed_hash = Path(key.name).stem.lower()
        actual_hash = sha256(content).hexdigest()
        if not self._CONTENT_HASH.fullmatch(claimed_hash) or claimed_hash != actual_hash:
            raise CoverAssetConflictError("封面内容与 SHA-256 存储键不一致")


def build_cover_asset_storage(
    *,
    backend: str,
    local_root: str | Path,
    staging_root: str | Path,
    oss_region: str = "",
    oss_endpoint: str = "",
    oss_bucket: str = "",
    oss_prefix: str = "cover-monitor/v1",
    oss_credential_mode: str = "ecs_ram_role",
    oss_ecs_role_name: str = "",
    oss_client: OssObjectClient | None = None,
) -> CoverAssetStorage:
    backend_name = str(backend or "local").strip().lower()
    if backend_name == "local":
        return LocalCoverAssetStorage(local_root)
    if backend_name != "oss":
        raise ValueError(f"不支持的封面存储后端: {backend_name}")
    region = str(oss_region or "").strip()
    if not re.fullmatch(r"[a-z0-9-]{3,64}", region):
        raise ValueError("OSS region 配置非法或缺失")
    endpoint = str(oss_endpoint or "").strip()
    if endpoint:
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.path not in {"", "/"}:
            raise ValueError("OSS endpoint 必须是 HTTPS 服务根地址")
    if oss_client is None:
        from service.cover_monitor.oss_client import build_aliyun_oss_v2_client

        oss_client = build_aliyun_oss_v2_client(
            region=region,
            endpoint=endpoint,
            credential_mode=oss_credential_mode,
            ecs_role_name=oss_ecs_role_name,
        )
    return OssCoverAssetStorage(
        client=oss_client,
        bucket=oss_bucket,
        prefix=oss_prefix,
        staging_root=staging_root,
    )
