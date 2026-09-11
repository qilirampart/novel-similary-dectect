from __future__ import annotations

import pytest

from service.cover_monitor.collector import CoverCollectionError, YouTubeChannelCollector


def test_collector_covers_videos_and_shorts_and_deduplicates_video_ids() -> None:
    calls: list[tuple[dict[str, object], str]] = []

    def extract(options: dict[str, object], url: str) -> dict:
        calls.append((options, url))
        common = {"id": "video001", "title": "重复视频"}
        if url.endswith("/videos"):
            return {
                "channel_id": "UC123456",
                "channel": "测试频道",
                "entries": [common, {"id": "video002", "title": "长视频"}],
            }
        return {"channel_id": "UC123456", "channel": "测试频道", "entries": [common, {"id": "short003"}]}

    result = YouTubeChannelCollector(
        proxy_url="http://127.0.0.1:17890",
        extract_info=extract,
    ).collect("https://youtube.com/@example/videos")

    assert result.completeness == "complete"
    assert result.scopes_completed == ("videos", "shorts")
    assert result.scope_item_counts == {"videos": 2, "shorts": 2}
    assert [item.video_id for item in result.videos] == ["video001", "video002", "short003"]
    assert [url for _, url in calls] == [
        "https://www.youtube.com/@example/videos",
        "https://www.youtube.com/@example/shorts",
    ]
    assert all(options["proxy"] == "http://127.0.0.1:17890" for options, _ in calls)


def test_collector_marks_partial_scope_without_discarding_success() -> None:
    def extract(_options: dict[str, object], url: str) -> dict:
        if url.endswith("/shorts"):
            raise RuntimeError("HTTP Error 403")
        return {"channel_id": "UC123456", "channel": "测试频道", "entries": [{"id": "video001"}]}

    result = YouTubeChannelCollector(extract_info=extract).collect("https://www.youtube.com/channel/UC123456")

    assert result.completeness == "partial"
    assert result.scopes_completed == ("videos",)
    assert "shorts" in result.scope_errors
    assert len(result.videos) == 1


def test_collector_treats_missing_shorts_tab_as_empty_completed_scope() -> None:
    def extract(_options: dict[str, object], url: str) -> dict:
        if url.endswith("/shorts"):
            raise RuntimeError("This channel does not have a shorts tab")
        return {"channel_id": "UC123456", "channel": "测试频道", "entries": [{"id": "video001"}]}

    result = YouTubeChannelCollector(extract_info=extract).collect(
        "https://www.youtube.com/channel/UC123456"
    )

    assert result.completeness == "complete"
    assert result.scopes_completed == ("videos", "shorts")
    assert result.scope_item_counts == {"videos": 1, "shorts": 0}
    assert result.scope_errors == {}


def test_collector_retries_only_transient_errors() -> None:
    attempts = 0

    def extract(_options: dict[str, object], _url: str) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("connection reset")
        return {"entries": []}

    result = YouTubeChannelCollector(extract_info=extract, sleep=lambda _seconds: None).collect(
        "https://www.youtube.com/@example", include_shorts=False
    )

    assert result.completeness == "complete"
    assert attempts == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/channel/UC123456",
        "https://www.youtube.com/watch?v=video001",
        "https://www.youtube.com/shorts/video001",
    ],
)
def test_collector_rejects_non_channel_urls(url: str) -> None:
    with pytest.raises(ValueError):
        YouTubeChannelCollector.normalize_channel_root(url)


def test_collector_raises_when_all_scopes_fail() -> None:
    collector = YouTubeChannelCollector(extract_info=lambda _options, _url: (_ for _ in ()).throw(RuntimeError("blocked")))
    with pytest.raises(CoverCollectionError, match="均采集失败"):
        collector.collect("https://www.youtube.com/@example")


def test_collector_marks_manual_limit_as_partial() -> None:
    collector = YouTubeChannelCollector(
        extract_info=lambda _options, _url: {"entries": [{"id": "video001"}]}
    )
    result = collector.collect(
        "https://www.youtube.com/@example",
        include_shorts=False,
        max_items_per_scope=1,
    )
    assert result.completeness == "partial"
    assert "达到人工采集上限" in result.scope_errors["videos"]
