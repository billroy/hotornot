import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as game


ANSWER = {
    "choice": "purgatory",
    "probabilities": {"heaven": 0.2, "hell": 0.1, "purgatory": 0.7},
    "confidence": 0.58,
}
TWO_OPTION_ANSWER = {
    "choice": "hell",
    "probabilities": {"heaven": 0.35, "hell": 0.65},
    "confidence": 0.72,
}


def wait_for_event(client, name, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in client.get_received():
            if event["name"] == name:
                return event["args"][0]
        time.sleep(0.01)
    pytest.fail(f"Did not receive {name}")


def wait_for_events(client, names, timeout=2):
    remaining = set(names)
    found = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in client.get_received():
            if event["name"] in remaining:
                found[event["name"]] = event["args"][0]
                remaining.remove(event["name"])
        if not remaining:
            return found
        time.sleep(0.01)
    pytest.fail(f"Did not receive {', '.join(sorted(remaining))}")


def test_typesafe_request_uses_unchanged_state_and_exact_choice(monkeypatch):
    seen = {}
    timestamps = iter([100.0, 100.1234])

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {
                "answers": {"destination": {"type": "choice", **ANSWER}},
                "usage": {"input_tokens": 17, "output_tokens": 3},
            }

    def fake_post(url, *, json, headers, timeout):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    monkeypatch.setattr(game.requests, "post", fake_post)
    monkeypatch.setattr(game.time, "monotonic", lambda: next(timestamps))
    subject = "  Ada Lovelace  "
    result = game.evaluate_subject(subject)
    assert {key: result[key] for key in ANSWER} == ANSWER
    assert result["service_response_duration_ms"] == 123.4
    assert result["token_usage"] == {"input_tokens": 17, "output_tokens": 3}
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["body"] == {
        "state": subject,
        "model": "jev-latest",
        "questions": {
            "destination": {
                "type": "choice",
                "instructions": "Where should this one go?",
                "criteria": {"heaven": None, "hell": None, "purgatory": None},
            }
        },
    }
    assert seen["headers"]["Authorization"] == "Bearer test-secret"
    assert seen["timeout"] > 0


def test_typesafe_request_can_omit_purgatory(monkeypatch):
    seen = {}

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {"answers": {"destination": {"type": "choice", **TWO_OPTION_ANSWER}}}

    def fake_post(url, *, json, headers, timeout):
        seen.update(body=json)
        return Response()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    monkeypatch.setattr(game.requests, "post", fake_post)
    result = game.evaluate_subject("coffee", enable_purgatory=False)
    assert {key: result[key] for key in TWO_OPTION_ANSWER} == TWO_OPTION_ANSWER
    assert "service_response_duration_ms" in result
    assert seen["body"]["questions"]["destination"]["criteria"] == {"heaven": None, "hell": None}


def test_typesafe_api_call_logging_is_off_by_default(monkeypatch, caplog):
    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {"answers": {"destination": {"type": "choice", **ANSWER}}}

    def fake_post(url, *, json, headers, timeout):
        return Response()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    monkeypatch.setattr(game.requests, "post", fake_post)
    caplog.set_level(logging.INFO, logger=game.LOGGER.name)

    game.evaluate_subject("coffee")

    assert "TypeSafe API request" not in caplog.text
    assert "TypeSafe API response" not in caplog.text
    assert "test-secret" not in caplog.text


def test_typesafe_api_call_logging_pretty_prints_without_api_key(monkeypatch, caplog):
    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {"answers": {"destination": {"type": "choice", **ANSWER}}}

    def fake_post(url, *, json, headers, timeout):
        assert headers["Authorization"] == "Bearer test-secret"
        return Response()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    monkeypatch.setattr(game.requests, "post", fake_post)
    caplog.set_level(logging.INFO, logger=game.LOGGER.name)

    game.evaluate_subject("coffee", log_api_calls=True)

    assert "TypeSafe API request" in caplog.text
    assert "TypeSafe API response" in caplog.text
    assert '"Authorization": "<redacted>"' in caplog.text
    assert '"state": "coffee"' in caplog.text
    assert '"status_code": 200' in caplog.text
    assert '"probabilities": {' in caplog.text
    assert "test-secret" not in caplog.text


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "choice", "choice": "heaven", "probabilities": {"heaven": 1}, "confidence": 1},
        {"type": "choice", "choice": "heaven", "probabilities": {"heaven": 0.5, "hell": 0.4, "purgatory": 0.1}, "confidence": float("nan")},
        {"type": "choice", "choice": "hell", "probabilities": {"heaven": 0.8, "hell": 0.1, "purgatory": 0.1}, "confidence": 0.5},
    ],
)
def test_invalid_typesafe_results_are_rejected(answer):
    with pytest.raises(ValueError):
        game.validate_answer({"answers": {"destination": answer}})


def test_two_option_typesafe_results_reject_purgatory():
    with pytest.raises(ValueError):
        game.validate_answer({"answers": {"destination": {"type": "choice", **ANSWER}}}, ("heaven", "hell"))


def test_token_usage_rejects_malformed_values():
    with pytest.raises(ValueError):
        game.validate_answer(
            {
                "answers": {"destination": {"type": "choice", **ANSWER}},
                "usage": {"input_tokens": -1},
            }
        )


def test_history_persists_and_promotes_jev_telemetry(tmp_path):
    evaluation = {
        **ANSWER,
        "service_response_duration_ms": 87.5,
        "token_usage": {"input_tokens": 21, "output_tokens": 4},
    }
    store = game.HistoryStore(tmp_path / "history.jsonl")

    saved = store.append("request-1", "coffee", evaluation)
    promoted = store.promote_cached("request-2", "coffee", True)

    assert saved["service_response_duration_ms"] == 87.5
    assert saved["token_usage"] == {"input_tokens": 21, "output_tokens": 4}
    assert promoted["service_response_duration_ms"] == 87.5
    assert promoted["token_usage"] == {"input_tokens": 21, "output_tokens": 4}
    assert game.HistoryStore(store.path).snapshot() == [promoted]


def test_broadcast_and_file_reload(tmp_path):
    history_file = tmp_path / "history.jsonl"
    app, socketio = game.create_app(history_file, evaluator=lambda subject: ANSWER)
    first = socketio.test_client(app)
    second = socketio.test_client(app)
    assert wait_for_event(first, "judgment:history") == {"results": []}
    assert wait_for_event(second, "judgment:history") == {"results": []}

    first.emit("judgment:submit", {"request_id": "request-1", "subject": "  pineapple on pizza  "})
    first_result = wait_for_event(first, "judgment:result")
    second_result = wait_for_event(second, "judgment:result")
    assert first_result == second_result
    assert first_result["subject"] == "  pineapple on pizza  "
    assert first_result["sequence"] == 1
    assert first_result["confidence"] == ANSWER["confidence"]
    assert len(history_file.read_text().splitlines()) == 1

    restarted_app, restarted_socketio = game.create_app(history_file, evaluator=lambda subject: ANSWER)
    late = restarted_socketio.test_client(restarted_app)
    assert wait_for_event(late, "judgment:history") == {"results": [first_result]}

    late.emit("judgment:submit", {"request_id": "request-2", "subject": "coffee"})
    second_verdict = wait_for_event(late, "judgment:result")
    assert second_verdict["sequence"] == 2
    assert len([json.loads(line) for line in history_file.read_text().splitlines()]) == 2


def test_connection_count_broadcasts_on_connect_and_disconnect(tmp_path):
    app, socketio = game.create_app(tmp_path / "history.jsonl", evaluator=lambda subject: ANSWER)
    first = socketio.test_client(app)
    assert wait_for_event(first, "connection:count") == {"count": 1}

    second = socketio.test_client(app)
    assert wait_for_event(first, "connection:count") == {"count": 2}
    assert wait_for_event(second, "connection:count") == {"count": 2}

    second.disconnect()
    assert wait_for_event(first, "connection:count") == {"count": 1}


def test_invalid_input_and_failure_do_not_enter_history(tmp_path):
    calls = []

    def evaluator(subject):
        calls.append(subject)
        raise game.JudgmentError("TypeSafe could not be reached. Please try again.")

    history_file = tmp_path / "history.jsonl"
    app, socketio = game.create_app(history_file, evaluator=evaluator)
    first = socketio.test_client(app)
    second = socketio.test_client(app)
    first.get_received()
    second.get_received()

    first.emit("judgment:submit", {"request_id": "bad", "subject": "   "})
    assert "Enter a name" in wait_for_event(first, "judgment:error")["message"]
    assert calls == []

    first.emit("judgment:submit", {"request_id": "fail", "subject": "coffee"})
    assert "TypeSafe could not be reached" in wait_for_event(first, "judgment:error")["message"]
    assert calls == ["coffee"]
    assert second.get_received() == []
    assert not history_file.exists()


def test_file_failure_is_reported_without_broadcast(tmp_path, monkeypatch):
    app, socketio = game.create_app(tmp_path / "history.jsonl", evaluator=lambda subject: ANSWER)
    first = socketio.test_client(app)
    second = socketio.test_client(app)
    first.get_received()
    second.get_received()

    def fail_append(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(app.extensions["history_store"], "append", fail_append)
    first.emit("judgment:submit", {"request_id": "file-fail", "subject": "coffee"})
    assert "judgment failed" in wait_for_event(first, "judgment:error")["message"].lower()
    assert second.get_received() == []
    assert app.extensions["history_store"].snapshot() == []


def test_concurrent_history_writes_are_complete_and_ordered(tmp_path):
    store = game.HistoryStore(tmp_path / "history.jsonl")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: store.append(str(index), f"subject {index}", ANSWER), range(25)))
    assert len({record["id"] for record in results}) == 25
    reloaded = game.HistoryStore(store.path).snapshot()
    assert [record["sequence"] for record in reloaded] == list(range(1, 26))


def test_history_cache_promotes_exact_subject_and_purgatory_flag(tmp_path, caplog):
    calls = []

    def evaluator(subject, enable_purgatory):
        calls.append((subject, enable_purgatory))
        return ANSWER if enable_purgatory else TWO_OPTION_ANSWER

    history_file = tmp_path / "history.jsonl"
    app, socketio = game.create_app(
        history_file,
        evaluator=evaluator,
        rate_limit_per_minute=0,
        rate_limit_per_day=0,
    )
    first = socketio.test_client(app)
    second = socketio.test_client(app)
    first.get_received()
    second.get_received()

    first.emit("judgment:submit", {"request_id": "request-1", "subject": "coffee", "enable_purgatory": False})
    wait_for_event(first, "judgment:result")
    wait_for_event(second, "judgment:result")
    first.emit("judgment:submit", {"request_id": "request-2", "subject": "coffee", "enable_purgatory": True})
    wait_for_event(first, "judgment:result")
    wait_for_event(second, "judgment:result")
    first.get_received()
    second.get_received()
    assert app.logger.getEffectiveLevel() <= logging.INFO
    caplog.set_level("INFO", logger=app.logger.name)

    first.emit("judgment:submit", {"request_id": "request-3", "subject": "coffee", "enable_purgatory": False})
    first_events = wait_for_events(first, {"judgment:result", "judgment:history"})
    second_events = wait_for_events(second, {"judgment:result", "judgment:history"})
    cached_result = first_events["judgment:result"]
    second_cached_result = second_events["judgment:result"]
    first_history = first_events["judgment:history"]
    second_history = second_events["judgment:history"]

    assert calls == [("coffee", False), ("coffee", True)]
    assert cached_result == second_cached_result
    assert cached_result["request_id"] == "request-3"
    assert cached_result["probabilities"] == TWO_OPTION_ANSWER["probabilities"]
    assert cached_result["sequence"] == 2
    assert first_history == second_history
    assert [result["request_id"] for result in first_history["results"]] == ["request-2", "request-3"]
    assert [result["sequence"] for result in first_history["results"]] == [1, 2]
    assert game.HistoryStore(history_file).snapshot() == first_history["results"]
    assert "History cache hit substituted for TypeSafe API call" in caplog.text
    assert "request_id=request-3" in caplog.text
    assert "enable_purgatory=False" in caplog.text
    assert "coffee" not in caplog.text


def test_history_cache_can_be_disabled(tmp_path):
    calls = []

    def evaluator(subject, enable_purgatory):
        calls.append((subject, enable_purgatory))
        return ANSWER

    app, socketio = game.create_app(
        tmp_path / "history.jsonl",
        evaluator=evaluator,
        rate_limit_per_minute=0,
        rate_limit_per_day=0,
        use_cache=False,
    )
    first = socketio.test_client(app)
    first.get_received()

    first.emit("judgment:submit", {"request_id": "request-1", "subject": "coffee"})
    wait_for_event(first, "judgment:result")
    first.emit("judgment:submit", {"request_id": "request-2", "subject": "coffee"})
    wait_for_event(first, "judgment:result")

    assert calls == [("coffee", False), ("coffee", False)]


def test_ip_rate_limiter_enforces_minute_and_day_windows():
    now = 1_000.0
    limiter = game.IpRateLimiter(per_minute=1, per_day=2, clock=lambda: now)

    assert limiter.allow("203.0.113.10")
    assert not limiter.allow("203.0.113.10")
    assert limiter.allow("203.0.113.11")

    now += 61
    assert limiter.allow("203.0.113.10")
    now += 61
    assert not limiter.allow("203.0.113.10")

    now += game.IpRateLimiter.DAY_SECONDS
    assert limiter.allow("203.0.113.10")


def test_rate_limited_submit_does_not_evaluate_or_enter_history(tmp_path):
    calls = []

    def evaluator(subject):
        calls.append(subject)
        return ANSWER

    history_file = tmp_path / "history.jsonl"
    app, socketio = game.create_app(
        history_file,
        evaluator=evaluator,
        rate_limit_per_minute=1,
        rate_limit_per_day=100,
    )
    flask_client = app.test_client()
    flask_client.environ_base["REMOTE_ADDR"] = "203.0.113.24"
    first = socketio.test_client(app, flask_test_client=flask_client)
    first.get_received()

    first.emit("judgment:submit", {"request_id": "request-1", "subject": "coffee"})
    wait_for_event(first, "judgment:result")
    first.emit("judgment:submit", {"request_id": "request-2", "subject": "tea"})
    error = wait_for_event(first, "judgment:error")

    assert "rate limit" in error["message"].lower()
    assert calls == ["coffee"]
    assert len(history_file.read_text().splitlines()) == 1


def test_page_serves_html_without_application_rest_api(tmp_path):
    app, _ = game.create_app(tmp_path / "history.jsonl", evaluator=lambda subject: ANSWER)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Heaven" in response.data
    assert client.get("/api/results").status_code == 404


def test_healthz_requires_typesafe_configuration(tmp_path, monkeypatch):
    app, _ = game.create_app(tmp_path / "history.jsonl", evaluator=lambda subject: ANSWER)
    client = app.test_client()
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert client.get("/healthz").status_code == 503
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    assert client.get("/healthz").json == {"status": "ok"}


def test_client_ip_uses_fly_proxy_header_only_on_fly(tmp_path, monkeypatch):
    app, _ = game.create_app(tmp_path / "history.jsonl", evaluator=lambda subject: ANSWER)
    headers = {"Fly-Client-IP": "203.0.113.24"}
    with app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": "192.0.2.10"}):
        monkeypatch.delenv("FLY_APP_NAME", raising=False)
        assert game.client_ip() == "192.0.2.10"
        monkeypatch.setenv("FLY_APP_NAME", "hotornot-test")
        assert game.client_ip() == "203.0.113.24"


def test_extract_proper_names_from_google_news_rss():
    feed = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>Five Takeaways From Trump - CNN</title>
        <description><![CDATA[
          <ol>
            <li><a href="one">Trump Says He Will Strike</a>&nbsp;&nbsp;<font>CNN</font></li>
            <li><a href="two">Donald Trump meets Marco Rubio in Washington</a>&nbsp;&nbsp;<font>The New York Times</font></li>
            <li><a href="three">Deal With Iran</a>&nbsp;&nbsp;<font>Reuters</font></li>
          </ol>
        ]]></description>
        <source>CNN</source>
      </item>
      <item>
        <title>President Ada Lovelace joined Sam Altman for remarks - Reuters</title>
        <source>Reuters</source>
      </item>
    </channel></rss>
    """

    assert game.extract_proper_names(feed) == ["Donald Trump", "Marco Rubio", "Ada Lovelace", "Sam Altman"]


def test_news_description_headlines_do_not_cross_article_or_publisher_boundaries():
    feed = """<rss><channel><item>
      <title>United Nations summit opens - CNN</title>
      <description><![CDATA[
        <ol>
          <li><a href="one">Fact check: Trump addresses the United Nations</a><font>CNN</font></li>
          <li><a href="two">At U.N., J.D. Vance delivers remarks</a><font>The New York Times</font></li>
        </ol>
      ]]></description>
      <source>CNN</source>
    </item></channel></rss>"""

    assert game.news_item_headlines(feed) == [
        "United Nations summit opens",
        "Fact check: Trump addresses the United Nations",
        "At U.N., J.D. Vance delivers remarks",
    ]
    assert game.extract_proper_names(feed) == ["J. D. Vance"]


def test_headline_fragments_are_not_names():
    feed = """<rss><channel>
      <item><title>Five Takeaways From Trump</title></item>
      <item><title>It The New York Times</title></item>
      <item><title>Deal With Iran</title></item>
      <item><title>Trump Says He Will Strike</title></item>
      <item><title>United Nations CNN At U</title></item>
      <item><title>CNN Fact</title></item>
    </channel></rss>"""

    assert game.extract_proper_names(feed) == []


def test_news_pump_refills_empty_queue_and_submits_at_random_mean_rate(caplog):
    submitted = []
    sleeps = []

    class FixedRandom:
        def expovariate(self, rate):
            assert rate == 1 / 300
            return 4.25

    feed = """<rss><channel>
      <item><title>Ada Lovelace and Grace Hopper honored - Google News</title></item>
    </channel></rss>"""
    pump = game.NewsPump(
        lambda request_id, subject: submitted.append((request_id, subject)),
        fetcher=lambda: feed,
        sleeper=sleeps.append,
        random_source=FixedRandom(),
    )
    caplog.set_level(logging.INFO, logger=game.LOGGER.name)

    assert pump.run_once()

    assert sleeps == [4.25]
    assert submitted[0][0].startswith("news-pump:")
    assert submitted[0][1] == "Ada Lovelace"
    assert pump.queue_snapshot() == ["Grace Hopper"]
    assert "News pump fetching news feed" in caplog.text
    assert "News pump sending fetched name to grid" in caplog.text
    assert "name=Ada Lovelace" in caplog.text


def test_news_pump_requeues_rejected_name_once():
    pump = game.NewsPump(lambda request_id, subject: None)

    assert pump.requeue("Ada Lovelace")
    assert not pump.requeue("Ada Lovelace")
    assert pump.queue_snapshot() == ["Ada Lovelace"]


def test_news_pump_seen_snapshot_is_an_independent_copy():
    feed = "<rss><channel><item><title>Ada Lovelace speaks</title></item></channel></rss>"
    pump = game.NewsPump(lambda request_id, subject: None, fetcher=lambda: feed)
    pump.refill_if_empty()

    snapshot = pump.seen_snapshot()
    snapshot.add("Grace Hopper")

    assert pump.seen_snapshot() == {"Ada Lovelace"}


def test_default_news_feed_urls_span_us_uk_and_eu():
    urls = game.DEFAULT_NEWS_FEED_URLS
    assert len(urls) >= 6
    assert game.DEFAULT_NEWS_FEED_URL == urls[0]
    assert any("gl=US" in url for url in urls)
    assert any("gl=GB" in url or "bbci.co.uk" in url for url in urls)
    assert any("france24.com" in url or "gl=IE" in url or "dw.com" in url for url in urls)


def test_combine_news_feeds_merges_items_and_skips_unparseable_feeds():
    us = (
        "<rss><channel>"
        "<item><title>Ada Lovelace wins award - BBC News</title>"
        "<source>BBC News</source></item>"
        "</channel></rss>"
    )
    uk = "<rss><channel><item><title>Grace Hopper honored</title></item></channel></rss>"
    dw = """<rdf:RDF
      xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/">
      <item rdf:about="https://www.dw.com/example">
        <title>Friedrich Merz addresses parliament</title>
        <description>Deutsche Welle report</description>
      </item>
    </rdf:RDF>"""
    broken = "this is not xml"

    combined = game.combine_news_feeds([us, uk, dw, broken])

    assert game.news_item_headlines(combined) == [
        "Ada Lovelace wins award",
        "Grace Hopper honored",
        "Friedrich Merz addresses parliament",
    ]


def test_news_item_headlines_reads_namespaced_rss_rdf():
    feed = """<rdf:RDF
      xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/">
      <item rdf:about="https://www.dw.com/example">
        <title>Friedrich Merz addresses parliament</title>
      </item>
    </rdf:RDF>"""

    assert game.news_item_headlines(feed) == ["Friedrich Merz addresses parliament"]
    assert game.extract_proper_names(feed) == ["Friedrich Merz"]


def test_fetch_news_feeds_tolerates_individual_source_failures(caplog):
    good = "<rss><channel><item><title>Ada Lovelace speaks</title></item></channel></rss>"

    def fake_fetch(url):
        if "bad.example" in url:
            raise game.requests.RequestException("boom")
        return good

    caplog.set_level(logging.WARNING, logger=game.LOGGER.name)
    observed = []
    combined = game.fetch_news_feeds(
        ["https://good.example/rss", "https://bad.example/rss"],
        fetcher=fake_fetch,
        feed_observer=lambda url, xml: observed.append((url, xml)),
    )

    assert game.news_item_headlines(combined) == ["Ada Lovelace speaks"]
    assert observed == [("https://good.example/rss", good)]
    assert "bad.example" in caplog.text


def test_news_subject_uses_shared_judgment_path_without_ip_rate_limit(tmp_path):
    calls = []
    app, socketio = game.create_app(
        tmp_path / "history.jsonl",
        evaluator=lambda subject, enable_purgatory: calls.append((subject, enable_purgatory)) or TWO_OPTION_ANSWER,
        rate_limit_per_minute=1,
        rate_limit_per_day=1,
    )
    first = socketio.test_client(app)
    first.get_received()

    app.extensions["submit_news_subject"]("news-pump:test", "Ada Lovelace")
    result = wait_for_event(first, "judgment:result")

    assert calls == [("Ada Lovelace", False)]
    assert result["request_id"] == "news-pump:test"
    assert result["subject"] == "Ada Lovelace"
    assert result["sequence"] == 1


def test_news_subject_uses_latest_browser_purgatory_preference(tmp_path):
    calls = []

    def evaluator(subject, enable_purgatory):
        calls.append((subject, enable_purgatory))
        return ANSWER if enable_purgatory else TWO_OPTION_ANSWER

    app, socketio = game.create_app(tmp_path / "history.jsonl", evaluator=evaluator)
    first = socketio.test_client(app)
    first.get_received()

    first.emit("purgatory:preference", {"enable_purgatory": True})
    app.extensions["submit_news_subject"]("news-pump:enabled", "Ada Lovelace")
    enabled_result = wait_for_event(first, "judgment:result")

    first.emit("purgatory:preference", {"enable_purgatory": False})
    app.extensions["submit_news_subject"]("news-pump:disabled", "Grace Hopper")
    disabled_result = wait_for_event(first, "judgment:result")

    assert calls == [("Ada Lovelace", True), ("Grace Hopper", False)]
    assert enabled_result["probabilities"] == ANSWER["probabilities"]
    assert disabled_result["probabilities"] == TWO_OPTION_ANSWER["probabilities"]


def test_submit_sends_browser_purgatory_preference_to_evaluator(tmp_path):
    calls = []

    def evaluator(subject, enable_purgatory):
        calls.append((subject, enable_purgatory))
        return TWO_OPTION_ANSWER

    app, socketio = game.create_app(tmp_path / "history.jsonl", evaluator=evaluator)
    first = socketio.test_client(app)
    first.get_received()

    first.emit("judgment:submit", {"request_id": "request-1", "subject": "coffee", "enable_purgatory": False})
    result = wait_for_event(first, "judgment:result")
    assert calls == [("coffee", False)]
    assert result["choice"] == "hell"
    assert result["probabilities"] == {"heaven": 0.35, "hell": 0.65}


def test_submit_defaults_purgatory_preference_to_disabled(tmp_path):
    calls = []

    def evaluator(subject, enable_purgatory):
        calls.append((subject, enable_purgatory))
        return ANSWER

    app, socketio = game.create_app(tmp_path / "history.jsonl", evaluator=evaluator)
    first = socketio.test_client(app)
    first.get_received()

    first.emit("judgment:submit", {"request_id": "request-1", "subject": "coffee"})
    wait_for_event(first, "judgment:result")
    assert calls == [("coffee", False)]


def test_cli_host_defaults_to_loopback_and_accepts_override():
    assert game.parse_args([]).host == "127.0.0.1"
    assert game.parse_args(["--host", "0.0.0.0"]).host == "0.0.0.0"


def test_cli_rate_limits_default_and_accept_overrides():
    defaults = game.parse_args([])
    assert defaults.rate_limit_per_minute == game.DEFAULT_RATE_LIMIT_PER_MINUTE
    assert defaults.rate_limit_per_day == game.DEFAULT_RATE_LIMIT_PER_DAY
    assert not defaults.no_cache
    assert not defaults.log_api_calls

    args = game.parse_args(["--rate-limit-per-minute", "2", "--rate-limit-per-day", "250"])
    assert args.rate_limit_per_minute == 2
    assert args.rate_limit_per_day == 250
    assert game.parse_args(["--no-cache"]).no_cache
    assert game.parse_args(["--log-api-calls"]).log_api_calls

    with pytest.raises(SystemExit):
        game.parse_args(["--rate-limit-per-minute", "-1"])


def test_cli_news_pump_interval_defaults_to_five_minutes_and_accepts_override():
    defaults = game.parse_args([])
    assert defaults.news_pump_interval == game.NEWS_PUMP_INTERVAL_SECONDS == 300.0

    assert game.parse_args(["--news-pump-interval", "120"]).news_pump_interval == 120.0
    assert game.parse_args(["--news-pump-mean-seconds", "45"]).news_pump_interval == 45.0

    with pytest.raises(SystemExit):
        game.parse_args(["--news-pump-interval", "0"])


def test_news_pump_interval_can_be_configured_by_environment(monkeypatch):
    monkeypatch.setenv("NEWS_PUMP_INTERVAL_SECONDS", "60")
    assert game.parse_args([]).news_pump_interval == 60.0

    monkeypatch.setenv("NEWS_PUMP_INTERVAL_SECONDS", "not-a-number")
    with pytest.raises(ValueError, match="NEWS_PUMP_INTERVAL_SECONDS must be a positive number"):
        game.parse_args([])
