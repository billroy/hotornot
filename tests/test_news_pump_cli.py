from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "news-pump.py"
SPEC = importlib.util.spec_from_file_location("standalone_news_pump", SCRIPT_PATH)
news_pump = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(news_pump)


class FakeClient:
    def __init__(self):
        self.connected = False
        self.connect_calls = []
        self.emits = []
        self.disconnect_calls = 0
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def trigger(self, event, payload=None):
        if payload is None:
            self.handlers[event]()
        else:
            self.handlers[event](payload)

    def connect(self, url):
        self.connect_calls.append(url)
        self.connected = True

    def emit(self, event, payload):
        self.emits.append((event, payload))

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


def test_cli_requires_url_and_defaults_to_300_seconds():
    args = news_pump.parse_args(["--url", "https://example.test"])
    assert args.url == "https://example.test"
    assert args.interval == 300.0
    assert not args.no_log
    assert not args.log_feed_stats

    args = news_pump.parse_args(
        ["--url", "https://example.test", "--log-feed-stats"]
    )
    assert args.log_feed_stats

    with pytest.raises(SystemExit):
        news_pump.parse_args([])
    with pytest.raises(SystemExit):
        news_pump.parse_args(["--url", "https://example.test", "--interval", "0"])


def test_socket_submitter_emits_existing_contract_and_logs_hit(capsys):
    client = FakeClient()
    submit = news_pump.SocketSubmitter(client)

    submit("news-pump:123", "Ada Lovelace")

    assert client.emits == [
        (
            "judgment:submit",
            {
                "request_id": "news-pump:123",
                "subject": "Ada Lovelace",
                "enable_purgatory": False,
            },
        )
    ]
    assert capsys.readouterr().out == "news-pump hit: Ada Lovelace\n"


def test_socket_submitter_no_log_suppresses_console_hit(capsys):
    client = FakeClient()
    submit = news_pump.SocketSubmitter(client, log_hits=False)

    submit("news-pump:123", "Ada Lovelace")

    assert len(client.emits) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached. Please try again later.",
        "The site is busy. Please try again shortly.",
        "Wait for the current judgment to finish.",
        "Too many judgments are already pending from this address. Please wait.",
    ],
)
def test_socket_submitter_requeues_temporary_rejections(message, capsys):
    client = FakeClient()
    retried = []
    submit = news_pump.SocketSubmitter(client)
    submit.set_retry_callback(retried.append)

    submit("news-pump:123", "Ada Lovelace")
    client.trigger(
        "judgment:error",
        {"request_id": "news-pump:123", "message": message},
    )

    assert retried == ["Ada Lovelace"]
    assert "news-pump retry queued: Ada Lovelace" in capsys.readouterr().out


@pytest.mark.parametrize("event_name", ["judgment:result", "judgment:complete"])
def test_socket_submitter_acknowledgment_clears_pending_name(event_name):
    client = FakeClient()
    retried = []
    submit = news_pump.SocketSubmitter(client, log_hits=False)
    submit.set_retry_callback(retried.append)

    submit("news-pump:123", "Ada Lovelace")
    client.trigger(event_name, {"request_id": "news-pump:123"})
    client.trigger("disconnect")

    assert retried == []


def test_socket_submitter_requeues_every_pending_name_on_disconnect():
    client = FakeClient()
    retried = []
    submit = news_pump.SocketSubmitter(client, log_hits=False)
    submit.set_retry_callback(retried.append)

    submit("news-pump:123", "Ada Lovelace")
    submit("news-pump:456", "Grace Hopper")
    client.trigger("disconnect")

    assert retried == ["Ada Lovelace", "Grace Hopper"]


def test_socket_submitter_requeues_name_when_emit_fails():
    class FailingClient(FakeClient):
        def emit(self, event, payload):
            raise OSError("connection lost")

    client = FailingClient()
    retried = []
    submit = news_pump.SocketSubmitter(client, log_hits=False)
    submit.set_retry_callback(retried.append)

    with pytest.raises(OSError):
        submit("news-pump:123", "Ada Lovelace")

    assert retried == ["Ada Lovelace"]


def test_feed_stats_logger_reports_raw_detections_and_per_feed_run_total(capsys):
    feed = """<rss><channel>
      <item><title>Ada Lovelace and Grace Hopper honored</title></item>
      <item><title>Ada Lovelace speaks again</title></item>
    </channel></rss>"""
    stats = news_pump.FeedStatsLogger()
    stats.set_seen_provider(lambda: {"Ada Lovelace"})

    stats("https://news.example/feed", feed)
    stats("https://news.example/feed", feed)
    stats("https://other.example/feed", feed)

    assert capsys.readouterr().out == (
        "news-pump feed stats: feed=https://news.example/feed names=2 unseen=1 "
        "raw_detections=3 raw_detections_since_start=3\n"
        "news-pump feed stats: feed=https://news.example/feed names=2 unseen=1 "
        "raw_detections=3 raw_detections_since_start=6\n"
        "news-pump feed stats: feed=https://other.example/feed names=2 unseen=1 "
        "raw_detections=3 raw_detections_since_start=3\n"
    )


def test_run_connects_pump_to_requested_server_and_disconnects():
    client = FakeClient()
    observed = {}

    class FakePump:
        def __init__(self, submit, mean_seconds, fetcher=None):
            observed["submit"] = submit
            observed["mean_seconds"] = mean_seconds
            observed["fetcher"] = fetcher
            observed["stopped"] = False

        def run(self):
            observed["ran"] = True

        def stop(self):
            observed["stopped"] = True

        def requeue(self, name):
            observed.setdefault("retried", []).append(name)

        def seen_snapshot(self):
            return {"Ada Lovelace"}

    args = news_pump.parse_args(
        ["--url", "https://example.test", "--interval", "42", "--no-log"]
    )
    news_pump.run(args, client=client, pump_factory=FakePump)

    assert client.connect_calls == ["https://example.test"]
    assert observed["mean_seconds"] == 42.0
    assert observed["ran"]
    assert observed["stopped"]
    assert observed["fetcher"] is None
    assert client.disconnect_calls == 1


def test_run_installs_feed_stats_fetcher_when_requested():
    client = FakeClient()
    observed = {}

    class FakePump:
        def __init__(self, submit, mean_seconds, fetcher=None):
            observed["fetcher"] = fetcher

        def run(self):
            pass

        def stop(self):
            pass

        def requeue(self, name):
            pass

        def seen_snapshot(self):
            return set()

    args = news_pump.parse_args(
        ["--url", "https://example.test", "--log-feed-stats"]
    )
    news_pump.run(args, client=client, pump_factory=FakePump)

    assert callable(observed["fetcher"])


def test_main_enables_info_logging(monkeypatch):
    logging_options = []

    monkeypatch.setattr(
        news_pump.logging,
        "basicConfig",
        lambda **options: logging_options.append(options),
    )
    monkeypatch.setattr(news_pump, "run", lambda args: None)

    assert news_pump.main(["--url", "https://example.test"]) == 0
    assert logging_options == [
        {"level": news_pump.logging.INFO, "format": "%(message)s"}
    ]
