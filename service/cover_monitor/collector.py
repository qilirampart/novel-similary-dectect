from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit


class CoverCollectionError(RuntimeError):
    pass


class CoverCollectionCancelled(CoverCollectionError):
    pass


@dataclass(frozen=True)
class CollectedCoverVideo:
    video_id: str
    title: str
    video_url: str
    thumbnail_url: str
    channel_id: str
    channel_name: str
    upload_date: Optional[str] = None


@dataclass(frozen=True)
class ChannelCollectionResult:
    source_url: str
    channel_id: str
    channel_name: str
    videos: tuple[CollectedCoverVideo, ...]
    scopes_completed: tuple[str, ...]
    scope_item_counts: dict[str, int]
    scope_errors: dict[str, str]

    @property
    def completeness(self) -> str:
        if not self.scope_errors:
            return "complete"
        return "partial" if self.scopes_completed else "failed"


ExtractInfo = Callable[[dict[str, object], str], dict[str, Any]]


class YouTubeChannelCollector:
    _VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
    _TRANSIENT_MARKERS = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
    )

    def __init__(
        self,
        *,
        proxy_url: str = "",
        cookie_path: str = "",
        timeout_seconds: float = 30,
        max_attempts: int = 2,
        extract_info: Optional[ExtractInfo] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.proxy_url = str(proxy_url or "").strip()
        self.cookie_path = Path(cookie_path) if str(cookie_path or "").strip() else None
        self.timeout_seconds = max(float(timeout_seconds), 1)
        self.max_attempts = max(int(max_attempts), 1)
        self._extract_info = extract_info
        self._sleep = sleep

    @staticmethod
    def normalize_channel_root(value: str) -> str:
        raw = str(value or "").strip()
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            raise ValueError("仅支持 YouTube 频道链接")
        path = parts.path.rstrip("/")
        if not path or path == "/watch" or path.startswith(("/shorts/", "/embed/")):
            raise ValueError("需要频道主页链接，不能使用单条视频链接")
        if path.endswith(("/videos", "/shorts", "/streams", "/featured", "/playlists")):
            path = path.rsplit("/", 1)[0]
        if not (
            path.startswith("/channel/UC")
            or path.startswith("/@")
            or path.startswith("/c/")
            or path.startswith("/user/")
        ):
            raise ValueError("无法识别 YouTube 频道链接")
        return urlunsplit(("https", "www.youtube.com", path, "", ""))

    def collect(
        self,
        source_url: str,
        *,
        max_items_per_scope: int = 0,
        include_shorts: bool = True,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> ChannelCollectionResult:
        root = self.normalize_channel_root(source_url)
        scopes = ["videos"] + (["shorts"] if include_shorts else [])
        collected: dict[str, CollectedCoverVideo] = {}
        completed: list[str] = []
        scope_item_counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        channel_id = ""
        channel_name = ""
        for scope in scopes:
            self._check_cancelled(should_cancel)
            try:
                payload = self._extract_with_retry(
                    f"{root}/{scope}",
                    max_items=max_items_per_scope,
                    should_cancel=should_cancel,
                )
                channel_id = str(payload.get("channel_id") or payload.get("uploader_id") or channel_id).strip()
                channel_name = str(
                    payload.get("channel") or payload.get("uploader") or payload.get("title") or channel_name
                ).strip()
                entries = list(payload.get("entries") or [])
                scope_item_counts[scope] = len(entries)
                for entry in entries:
                    item = self._normalize_entry(entry, channel_id=channel_id, channel_name=channel_name)
                    if item is not None:
                        collected.setdefault(item.video_id, item)
                completed.append(scope)
                skipped_count = sum(entry is None for entry in entries)
                if skipped_count:
                    errors[scope] = f"采集器跳过 {skipped_count} 个无法解析的条目"
                elif max_items_per_scope > 0 and len(entries) >= max_items_per_scope:
                    errors[scope] = f"达到人工采集上限 {max_items_per_scope}，范围可能不完整"
            except CoverCollectionCancelled:
                raise
            except Exception as exc:
                if self._scope_is_absent(exc, scope):
                    completed.append(scope)
                    scope_item_counts[scope] = 0
                    continue
                errors[scope] = self._safe_error(exc)
        if not completed:
            raise CoverCollectionError("频道的 Videos 和 Shorts 均采集失败")
        return ChannelCollectionResult(
            source_url=root,
            channel_id=channel_id,
            channel_name=channel_name,
            videos=tuple(collected.values()),
            scopes_completed=tuple(completed),
            scope_item_counts=scope_item_counts,
            scope_errors=errors,
        )

    def _options(self, *, max_items: int) -> dict[str, object]:
        options: dict[str, object] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            # Keep tab-level failures observable so an absent tab can be
            # distinguished from network or parser failures.
            "ignoreerrors": False,
            "socket_timeout": self.timeout_seconds,
        }
        if max_items > 0:
            options["playlistend"] = int(max_items)
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        if self.cookie_path and self.cookie_path.is_file() and self.cookie_path.stat().st_size > 0:
            options["cookiefile"] = str(self.cookie_path)
        return options

    def _extract_with_retry(
        self,
        url: str,
        *,
        max_items: int,
        should_cancel: Optional[Callable[[], bool]],
    ) -> dict[str, Any]:
        options = self._options(max_items=max_items)
        for attempt in range(1, self.max_attempts + 1):
            self._check_cancelled(should_cancel)
            try:
                if self._extract_info is not None:
                    payload = self._extract_info(options, url)
                else:
                    from yt_dlp import YoutubeDL

                    with YoutubeDL(options) as downloader:
                        payload = downloader.extract_info(url, download=False)
                if not isinstance(payload, dict):
                    raise CoverCollectionError("频道未返回结构化数据")
                return payload
            except CoverCollectionCancelled:
                raise
            except Exception as exc:
                if attempt >= self.max_attempts or not self._is_transient(exc):
                    raise
                self._sleep(min(float(attempt * 2), 5))
        raise CoverCollectionError("频道采集重试后仍未返回结果")

    @classmethod
    def _normalize_entry(
        cls,
        entry: Any,
        *,
        channel_id: str,
        channel_name: str,
    ) -> Optional[CollectedCoverVideo]:
        if not isinstance(entry, dict):
            return None
        video_id = str(entry.get("id") or "").strip()
        if not cls._VIDEO_ID.fullmatch(video_id):
            return None
        resolved_channel_id = str(entry.get("channel_id") or channel_id).strip()
        resolved_channel_name = str(entry.get("channel") or entry.get("uploader") or channel_name).strip()
        thumbnail = str(entry.get("thumbnail") or "").strip()
        if not thumbnail:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        return CollectedCoverVideo(
            video_id=video_id,
            title=str(entry.get("title") or video_id).strip(),
            video_url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=thumbnail,
            channel_id=resolved_channel_id,
            channel_name=resolved_channel_name,
            upload_date=str(entry.get("upload_date") or "").strip() or None,
        )

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(marker in message for marker in cls._TRANSIENT_MARKERS)

    @staticmethod
    def _scope_is_absent(exc: Exception, scope: str) -> bool:
        message = str(exc).casefold()
        return f"does not have a {scope.casefold()} tab" in message

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {str(exc)[:400]}"

    @staticmethod
    def _check_cancelled(should_cancel: Optional[Callable[[], bool]]) -> None:
        if should_cancel is not None and should_cancel():
            raise CoverCollectionCancelled("频道采集已取消")
