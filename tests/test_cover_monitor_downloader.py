from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from service.cover_monitor.downloader import CoverDownloadError, YouTubeCoverDownloader


def _jpeg(width: int = 480, height: int = 360) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color=(24, 120, 180)).save(output, format="JPEG", quality=85)
    return output.getvalue()


class FakeResponse:
    def __init__(self, content: bytes, *, url: str, content_type: str = "image/jpeg") -> None:
        self.content = content
        self.url = url
        self.status = 200
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.content if limit < 0 else self.content[:limit]

    def geturl(self) -> str:
        return self.url


class FakeOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout: float):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_downloader_falls_back_from_small_placeholder_and_versions_by_hash(tmp_path: Path) -> None:
    video_id = "video001"
    original = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    opener = FakeOpener([
        FakeResponse(_jpeg(120, 90), url=original),
        FakeResponse(_jpeg(), url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"),
    ])
    downloader = YouTubeCoverDownloader(tmp_path, opener=opener)

    result = downloader.download(video_id, original)

    assert result.width == 480
    assert result.height == 360
    assert result.fetched_url.endswith("/hqdefault.jpg")
    assert Path(result.local_path).is_file()
    assert result.content_sha256 in result.storage_key
    assert len(opener.requests) == 2


def test_downloader_rejects_untrusted_original_url() -> None:
    candidates = YouTubeCoverDownloader.candidate_urls(
        "video001",
        "https://attacker.example/vi/video001/hqdefault.jpg",
    )
    assert all("attacker.example" not in item for item in candidates)


def test_downloader_rejects_invalid_image_payload(tmp_path: Path) -> None:
    opener = FakeOpener([
        FakeResponse(b"x" * 2048, url="https://i.ytimg.com/vi/video001/maxresdefault.jpg"),
        FakeResponse(b"x" * 2048, url="https://i.ytimg.com/vi/video001/hqdefault.jpg"),
    ])
    with pytest.raises(CoverDownloadError) as error:
        YouTubeCoverDownloader(tmp_path, opener=opener).download("video001")
    assert len(error.value.attempts) == 2


def test_downloader_rejects_invalid_video_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        YouTubeCoverDownloader(tmp_path).download("../../etc/passwd")


def test_downloader_rejects_untrusted_final_redirect(tmp_path: Path) -> None:
    opener = FakeOpener([
        FakeResponse(_jpeg(), url="https://attacker.example/cover.jpg"),
        FakeResponse(_jpeg(), url="https://attacker.example/cover.jpg"),
    ])
    with pytest.raises(CoverDownloadError):
        YouTubeCoverDownloader(tmp_path, opener=opener).download("video001")
