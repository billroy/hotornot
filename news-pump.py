#!/usr/bin/env python3
"""Standalone news pump that submits discovered names over Socket.IO."""

from __future__ import annotations

import sys
import threading
from argparse import ArgumentParser, Namespace
from collections.abc import Callable

import socketio

from app import (
    NEWS_PUMP_INTERVAL_SECONDS,
    NewsPump,
    detect_proper_names,
    extract_proper_names,
    fetch_news_feeds,
    positive_float,
)


class FeedStatsLogger:
    """Report per-feed extraction counts against the pump's seen-name set."""

    def __init__(self) -> None:
        self._seen_provider: Callable[[], set[str]] = set
        self._detection_totals: dict[str, int] = {}

    def set_seen_provider(self, provider: Callable[[], set[str]]) -> None:
        self._seen_provider = provider

    def __call__(self, url: str, feed_xml: str) -> None:
        detections = detect_proper_names(feed_xml)
        names = extract_proper_names(feed_xml)
        seen = self._seen_provider()
        unseen = sum(name not in seen for name in names)
        total = self._detection_totals.get(url, 0) + len(detections)
        self._detection_totals[url] = total
        print(
            f"news-pump feed stats: feed={url} names={len(names)} unseen={unseen} "
            f"raw_detections={len(detections)} raw_detections_since_start={total}",
            flush=True,
        )


class SocketSubmitter:
    """Submit names and retain them until the server acknowledges each request."""

    def __init__(self, client: socketio.Client, log_hits: bool = True) -> None:
        self.client = client
        self.log_hits = log_hits
        self._pending: dict[str, str] = {}
        self._retry: Callable[[str], object] | None = None
        self._lock = threading.Lock()
        self._closing = False
        self.client.on("judgment:result", self._on_result)
        self.client.on("judgment:complete", self._on_complete)
        self.client.on("judgment:error", self._on_error)
        self.client.on("disconnect", self._on_disconnect)

    def set_retry_callback(self, retry: Callable[[str], object]) -> None:
        self._retry = retry

    def close(self) -> None:
        """Mark an intentional shutdown so disconnect does not schedule retries."""
        with self._lock:
            self._closing = True
            self._pending.clear()

    def _take_pending(self, request_id: object) -> str | None:
        if not isinstance(request_id, str):
            return None
        with self._lock:
            return self._pending.pop(request_id, None)

    def _retry_request(self, request_id: object, reason: str) -> None:
        retry = self._retry
        if retry is None:
            return
        name = self._take_pending(request_id)
        if name is None:
            return
        retry(name)
        if self.log_hits:
            print(f"news-pump retry queued: {name} ({reason})", flush=True)

    def _on_result(self, payload: object) -> None:
        if isinstance(payload, dict):
            self._take_pending(payload.get("request_id"))

    def _on_complete(self, payload: object) -> None:
        if isinstance(payload, dict):
            self._take_pending(payload.get("request_id"))

    def _on_error(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        message = payload.get("message")
        normalized = message.lower() if isinstance(message, str) else ""
        temporary_markers = (
            "rate limit",
            "site is busy",
            "wait for the current judgment",
            "too many judgments",
            "too many people",
        )
        if any(marker in normalized for marker in temporary_markers):
            self._retry_request(payload.get("request_id"), message or "temporary rejection")
        else:
            self._take_pending(payload.get("request_id"))

    def _on_disconnect(self, *_args: object) -> None:
        retry = self._retry
        with self._lock:
            if self._closing or retry is None:
                return
            names = list(self._pending.values())
            self._pending.clear()
        for name in names:
            retry(name)
            if self.log_hits:
                print(f"news-pump retry queued: {name} (connection lost)", flush=True)

    def __call__(self, request_id: str, name: str) -> None:
        if self.log_hits:
            print(f"news-pump hit: {name}", flush=True)
        with self._lock:
            self._pending[request_id] = name
        try:
            self.client.emit(
                "judgment:submit",
                {
                    "request_id": request_id,
                    "subject": name,
                    "enable_purgatory": False,
                },
            )
        except Exception:
            self._retry_request(request_id, "send failed")
            raise


def parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description="Fetch news names and submit them to a Heaven or Hell server.")
    parser.add_argument(
        "--url",
        required=True,
        help="Socket.IO server URL, for example https://hotornot.example.com",
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=NEWS_PUMP_INTERVAL_SECONDS,
        help="mean seconds between submissions (default: 300)",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="do not log each surfaced name to the console",
    )
    parser.add_argument(
        "--log-feed-stats",
        action="store_true",
        help="log matched and previously unseen name counts for each feed",
    )
    return parser.parse_args(argv)


def run(
    args: Namespace,
    client: socketio.Client | None = None,
    pump_factory: Callable[..., NewsPump] = NewsPump,
) -> None:
    socket_client = client or socketio.Client(reconnection=True)
    submit = SocketSubmitter(socket_client, log_hits=not args.no_log)
    feed_stats = FeedStatsLogger() if args.log_feed_stats else None
    pump_options = {"mean_seconds": args.interval}
    if feed_stats is not None:
        pump_options["fetcher"] = lambda: fetch_news_feeds(feed_observer=feed_stats)
    pump = pump_factory(submit, **pump_options)
    submit.set_retry_callback(pump.requeue)
    if feed_stats is not None:
        feed_stats.set_seen_provider(pump.seen_snapshot)

    socket_client.connect(args.url)
    try:
        pump.run()
    finally:
        submit.close()
        pump.stop()
        if socket_client.connected:
            socket_client.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        return 0
    except (socketio.exceptions.ConnectionError, OSError) as exc:
        print(f"news-pump: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
