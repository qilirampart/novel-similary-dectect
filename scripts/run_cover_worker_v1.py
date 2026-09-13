from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import SETTINGS
from service.cover_monitor.collector import YouTubeChannelCollector
from service.cover_monitor.downloader import YouTubeCoverDownloader
from service.cover_monitor.executor import CoverRunExecutor
from service.cover_monitor.reviewer import CoverVisionProfile, CoverVisionReviewer
from service.cover_monitor.store import CoverMonitorStore
from service.cover_monitor.worker import run_cover_worker_loop


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the independent cover-monitor worker")
    parser.add_argument("--once", action="store_true", help="Poll at most one run and exit")
    parser.add_argument("--db", default=SETTINGS.cover_monitor_db_path)
    parser.add_argument("--asset-root", default=SETTINGS.cover_monitor_asset_root)
    parser.add_argument("--worker-name", default=SETTINGS.cover_monitor_worker_name)
    parser.add_argument("--poll-seconds", type=float, default=SETTINGS.cover_monitor_worker_poll_seconds)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    SETTINGS.ensure_runtime_dirs()
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logging.getLogger(__name__).info("cover worker received signal=%s", signum)
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, request_stop)

    store = CoverMonitorStore(args.db)
    collector = YouTubeChannelCollector(
        proxy_url=SETTINGS.cover_monitor_proxy_url,
        cookie_path=SETTINGS.cover_monitor_cookie_path,
        timeout_seconds=SETTINGS.cover_monitor_network_timeout_seconds,
    )
    storage = SETTINGS.build_cover_asset_storage(local_root=args.asset_root)
    downloader = YouTubeCoverDownloader(
        storage=storage,
        proxy_url=SETTINGS.cover_monitor_proxy_url,
        timeout_seconds=SETTINGS.cover_monitor_network_timeout_seconds,
        max_bytes=SETTINGS.cover_monitor_download_max_bytes,
        max_pixels=SETTINGS.cover_monitor_image_max_pixels,
    )
    reviewer = CoverVisionReviewer(
        CoverVisionProfile(
            api_base=SETTINGS.cover_monitor_vision_api_base,
            api_key=SETTINGS.cover_monitor_vision_api_key,
            model=SETTINGS.cover_monitor_vision_model,
            timeout_seconds=SETTINGS.cover_monitor_vision_timeout_seconds,
        ),
        max_image_bytes=SETTINGS.cover_monitor_download_max_bytes,
    )
    worker_name = f"{args.worker_name}-{os.getpid()}"
    executor = CoverRunExecutor(
        store=store,
        collector=collector,
        downloader=downloader,
        reviewer=reviewer,
        worker_name=worker_name,
        max_attempts=SETTINGS.cover_monitor_worker_max_attempts,
        retry_delay_seconds=SETTINGS.cover_monitor_worker_retry_seconds,
        heartbeat_interval_seconds=SETTINGS.cover_monitor_worker_heartbeat_seconds,
        should_stop=stop_event.is_set,
    )
    processed = run_cover_worker_loop(
        store=store,
        executor=executor,
        stop_event=stop_event,
        once=args.once,
        poll_seconds=args.poll_seconds,
        stale_after_seconds=SETTINGS.cover_monitor_worker_stale_seconds,
        recovery_sweep_seconds=SETTINGS.cover_monitor_worker_recovery_sweep_seconds,
    )
    logging.getLogger(__name__).info("cover worker stopped processed=%s", processed)


if __name__ == "__main__":
    main()
