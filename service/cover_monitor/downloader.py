from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image, UnidentifiedImageError

from service.cover_monitor.storage import CoverAssetStorage, LocalCoverAssetStorage


class CoverDownloadError(RuntimeError):
    def __init__(self, message: str, *, attempts: Optional[list[dict[str, str]]] = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


@dataclass(frozen=True)
class DownloadedCover:
    video_id: str
    original_url: str
    fetched_url: str
    local_path: str
    storage_key: str
    content_sha256: str
    mime_type: str
    byte_size: int
    width: int
    height: int


class YouTubeCoverDownloader:
    _VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
    _TRUSTED_HOSTS = {"i.ytimg.com", "img.youtube.com"}
    _USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

    def __init__(
        self,
        asset_root: str | Path | None = None,
        *,
        storage: CoverAssetStorage | None = None,
        proxy_url: str = "",
        timeout_seconds: float = 30,
        max_bytes: int = 12 * 1024 * 1024,
        max_pixels: int = 40_000_000,
        min_width: int = 200,
        min_height: int = 100,
        opener: Any = None,
    ) -> None:
        if storage is None:
            if asset_root is None:
                raise ValueError("asset_root or storage is required")
            storage = LocalCoverAssetStorage(asset_root)
        self.storage = storage
        self.asset_root = getattr(storage, "root", None)
        self.proxy_url = str(proxy_url or "").strip()
        self.timeout_seconds = max(float(timeout_seconds), 1)
        self.max_bytes = max(int(max_bytes), 1024)
        self.max_pixels = max(int(max_pixels), 1)
        self.min_width = max(int(min_width), 1)
        self.min_height = max(int(min_height), 1)
        self._opener = opener or build_opener(
            ProxyHandler({"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {})
        )

    @classmethod
    def candidate_urls(cls, video_id: str, original_url: str = "") -> tuple[str, ...]:
        cls._validate_video_id(video_id)
        candidates: list[str] = []
        original = str(original_url or "").strip()
        if original and cls._is_trusted_thumbnail(original, video_id):
            candidates.append(original)
        candidates.extend([
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        ])
        return tuple(dict.fromkeys(candidates))

    def download(self, video_id: str, original_url: str = "") -> DownloadedCover:
        self._validate_video_id(video_id)
        attempts: list[dict[str, str]] = []
        for url in self.candidate_urls(video_id, original_url):
            try:
                return self._download_candidate(video_id, original_url or url, url)
            except Exception as exc:
                attempts.append({
                    "url": url,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                })
        raise CoverDownloadError("所有 YouTube 封面候选地址均下载失败", attempts=attempts)

    def _download_candidate(self, video_id: str, original_url: str, url: str) -> DownloadedCover:
        request = Request(
            url,
            headers={"User-Agent": self._USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        )
        with self._opener.open(request, timeout=self.timeout_seconds) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise CoverDownloadError(f"响应类型不是支持的图片: {content_type or 'missing'}")
            declared_length = str(response.headers.get("Content-Length") or "").strip()
            if declared_length.isdigit() and int(declared_length) > self.max_bytes:
                raise CoverDownloadError("封面响应超过字节上限")
            content = response.read(self.max_bytes + 1)
            fetched_url = str(response.geturl() or url)
        if not self._is_trusted_thumbnail(fetched_url, video_id):
            raise CoverDownloadError("封面最终地址不是受信任的 YouTube 缩略图地址")
        if len(content) > self.max_bytes:
            raise CoverDownloadError("封面响应超过字节上限")
        if len(content) < 1024:
            raise CoverDownloadError("封面文件过小")
        width, height, image_format = self._validate_image(content)
        suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[image_format]
        digest = sha256(content).hexdigest()
        storage_key = f"{video_id}/{digest}{suffix}"
        target = self.storage.put_bytes(storage_key, content)
        return DownloadedCover(
            video_id=video_id,
            original_url=str(original_url),
            fetched_url=fetched_url,
            local_path=str(target.resolve()),
            storage_key=storage_key.replace("\\", "/"),
            content_sha256=digest,
            mime_type=content_type,
            byte_size=len(content),
            width=width,
            height=height,
        )

    def _validate_image(self, content: bytes) -> tuple[int, int, str]:
        try:
            with Image.open(BytesIO(content)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if width * height > self.max_pixels:
                    raise CoverDownloadError(f"封面像素超过上限: {width}x{height}")
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise CoverDownloadError("封面图片无法解码") from exc
        if image_format not in {"JPEG", "PNG", "WEBP"}:
            raise CoverDownloadError(f"不支持的封面格式: {image_format or 'unknown'}")
        if width < self.min_width or height < self.min_height:
            raise CoverDownloadError(f"封面尺寸过小: {width}x{height}")
        return width, height, image_format

    @classmethod
    def _validate_video_id(cls, video_id: str) -> None:
        if not cls._VIDEO_ID.fullmatch(str(video_id or "").strip()):
            raise ValueError("非法 YouTube video_id")

    @classmethod
    def _is_trusted_thumbnail(cls, url: str, video_id: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and (parts.hostname or "").lower() in cls._TRUSTED_HOSTS
            and f"/{video_id}/" in parts.path
        )
