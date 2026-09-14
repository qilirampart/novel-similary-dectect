from __future__ import annotations

import pytest

from scripts.smoke_cloud_cover_real10_v1 import TERMINAL_STATUSES, extract_channel_id


def test_extract_channel_id_from_channel_url() -> None:
    assert extract_channel_id(
        "https://www.youtube.com/channel/UCX8oe3DG5lg7FYWguvuGW0Q"
    ) == "UCX8oe3DG5lg7FYWguvuGW0Q"


def test_extract_channel_id_rejects_handle_url() -> None:
    with pytest.raises(ValueError):
        extract_channel_id("https://www.youtube.com/@example")


def test_terminal_statuses_cover_success_failure_and_cancellation() -> None:
    assert {"completed", "partial_failed", "failed", "cancelled"} <= TERMINAL_STATUSES
