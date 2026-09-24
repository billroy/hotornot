"""Flask and Socket.IO server for the Heaven or Hell site."""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import random
import re
import threading
import time
import uuid
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections import OrderedDict, deque
from datetime import datetime, timezone
from functools import lru_cache
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from collections.abc import Callable, Iterable
from inspect import signature
from xml.etree import ElementTree

import requests
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit


DESTINATIONS = ("heaven", "hell", "purgatory")
CORE_DESTINATIONS = ("heaven", "hell")
QUESTION_ID = "destination"
MAX_SUBJECT_LENGTH = 200
MAX_CONCURRENT_EVALUATIONS = 4
MAX_QUEUED_EVALUATIONS = 32
MAX_QUEUE_WAIT_SECONDS = 10.0
MAX_OUTSTANDING_EVALUATIONS_PER_IP = 4
MAX_SUBSCRIBERS_PER_EVALUATION = 64
MAX_TRACKED_RATE_LIMIT_IPS = 10_000
MAX_RECENT_REQUEST_RESPONSES = 2_048
DEFAULT_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_RATE_LIMIT_PER_DAY = 500
# Responsible, mainstream news sources spanning the US, UK, EU, and India. Google News
# regional editions aggregate many outlets in the exact RSS shape the parser
# already handles; the direct outlet feeds broaden the range of sources. Feeds
# are fetched independently and merged, so an outlet that is unreachable or in an
# unexpected format simply contributes nothing (see fetch_news_feeds).
DEFAULT_NEWS_FEED_URLS = (
    # United States
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.pbs.org/newshour/feeds/rss/headlines",
    "https://moxie.foxnews.com/google-publisher/latest.xml",
    "https://www.latimes.com/world-nation/rss2.0.xml",
    # United Kingdom
    "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://www.independent.co.uk/news/uk/rss",
    "https://www.standard.co.uk/rss",
    "https://www.economist.com/international/rss.xml",
    # European Union
    "https://news.google.com/rss?hl=en-IE&gl=IE&ceid=IE:en",
    "https://www.france24.com/en/rss",
    "https://rss.dw.com/rdf/rss-en-all",
    # India
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
)
DEFAULT_NEWS_FEED_URL = DEFAULT_NEWS_FEED_URLS[0]
NEWS_PUMP_INTERVAL_SECONDS = 300.0
NEWS_PUMP_FETCH_BACKOFF_SECONDS = 60.0
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
LOGGER = logging.getLogger(__name__)
PERSON_INDEX_PATH = Path(__file__).with_name("names") / "person_index.json.gz"
NEWS_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[.’'\-][^\W_]+)*\.?", re.UNICODE)
MAX_PERSON_NAME_TOKENS = 6


class JudgmentError(Exception):
    """A failure that can be reported safely to the submitting browser."""


def destinations_for(enable_purgatory: bool) -> tuple[str, ...]:
    return DESTINATIONS if enable_purgatory else CORE_DESTINATIONS


def pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def evaluate_subject(subject: str, enable_purgatory: bool = True, log_api_calls: bool = False) -> dict:
    """Ask Jev one Choice question about the unmodified subject."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise JudgmentError("The site is not configured with a TypeSafe API key.")

    destinations = destinations_for(enable_purgatory)
    body = {
        "state": subject,
        "model": "jev-latest",
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": "Where should this one go?",
                "criteria": {destination: None for destination in destinations},
            }
        },
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for attempt in range(3):
        if log_api_calls:
            LOGGER.info(
                "TypeSafe API request:\n%s",
                pretty_json(
                    {
                        "method": "POST",
                        "url": TYPESAFE_URL,
                        "headers": {"Authorization": "<redacted>", "Content-Type": headers["Content-Type"]},
                        "body": body,
                    }
                ),
            )
        try:
            started_at = time.monotonic()
            response = requests.post(TYPESAFE_URL, json=body, headers=headers, timeout=25)
            response_duration_ms = round((time.monotonic() - started_at) * 1000, 3)
        except requests.RequestException as exc:
            raise JudgmentError("TypeSafe could not be reached. Please try again.") from exc

        if response.status_code in (429, 529) and attempt < 2:
            time.sleep(0.5 * (2**attempt))
            continue
        if response.status_code == 401:
            raise JudgmentError("The TypeSafe API key was rejected.")
        if not response.ok:
            raise JudgmentError("TypeSafe could not complete the judgment. Please try again.")
        try:
            response_body = response.json()
            if log_api_calls:
                LOGGER.info(
                    "TypeSafe API response:\n%s",
                    pretty_json({"status_code": response.status_code, "body": response_body}),
                )
            evaluation = validate_answer(response_body, destinations)
            evaluation["service_response_duration_ms"] = response_duration_ms
            return evaluation
        except (ValueError, TypeError) as exc:
            raise JudgmentError("TypeSafe returned an invalid result. Please try again.") from exc

    raise JudgmentError("TypeSafe is busy. Please try again shortly.")


def validate_answer(response: object, destinations: tuple[str, ...] = DESTINATIONS) -> dict:
    """Extract the Choice answer without inventing missing values."""
    if not isinstance(response, dict):
        raise ValueError("Response must be an object")
    answers = response.get("answers")
    answer = answers.get(QUESTION_ID) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("Missing Choice answer")

    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    confidence = answer.get("confidence")
    if choice not in destinations or not isinstance(probabilities, dict):
        raise ValueError("Invalid Choice shape")
    if set(probabilities) != set(destinations):
        raise ValueError("Missing or unexpected destination")

    def valid_number(value: object) -> bool:
        return type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1

    if not all(valid_number(probabilities[key]) for key in destinations):
        raise ValueError("Invalid probability")
    if not valid_number(confidence):
        raise ValueError("Invalid confidence")
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.001):
        raise ValueError("Probabilities do not sum to one")
    if probabilities[choice] < max(probabilities.values()) - 0.001:
        raise ValueError("Selected choice is not a highest-probability option")

    result = {
        "choice": choice,
        "probabilities": {key: float(probabilities[key]) for key in destinations},
        "confidence": float(confidence),
    }
    token_usage = normalize_token_usage(response.get("usage"))
    if token_usage is not None:
        result["token_usage"] = token_usage
    return result


def normalize_token_usage(usage: object) -> dict | None:
    """Return nonnegative token counters from the provider usage block."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("Invalid token usage")

    token_usage = {}
    for key, value in usage.items():
        if not isinstance(key, str) or not key.endswith("_tokens"):
            continue
        if type(value) is not int or value < 0:
            raise ValueError("Invalid token usage")
        token_usage[key] = value
    return token_usage or None


def telemetry_from_evaluation(evaluation: dict) -> dict:
    telemetry = {}
    token_usage = evaluation.get("token_usage")
    if isinstance(token_usage, dict):
        telemetry["token_usage"] = dict(token_usage)
    service_response_duration_ms = evaluation.get("service_response_duration_ms")
    if (
        type(service_response_duration_ms) in (float, int)
        and math.isfinite(service_response_duration_ms)
        and service_response_duration_ms >= 0
    ):
        telemetry["service_response_duration_ms"] = float(service_response_duration_ms)
    return telemetry


def accepts_enable_purgatory(evaluator: Callable) -> bool:
    try:
        parameters = signature(evaluator).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters) or len(
        [
            parameter
            for parameter in parameters
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
    ) >= 2


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_positive_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def fetch_news_feed(url: str = DEFAULT_NEWS_FEED_URL) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": "hotornot-news-pump/1.0"},
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def xml_local_name(tag: str) -> str:
    """Return an XML element name without its optional namespace."""
    return tag.rsplit("}", 1)[-1]


def news_feed_items(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Find RSS items in both plain RSS and namespaced RSS/RDF documents."""
    return [element for element in root.iter() if xml_local_name(element.tag) == "item"]


def child_text(element: ElementTree.Element, name: str) -> str:
    """Read a direct child by local name, regardless of its XML namespace."""
    for child in element:
        if xml_local_name(child.tag) == name:
            return child.text or ""
    return ""


def combine_news_feeds(feed_xmls: Iterable[str]) -> str:
    """Merge the <item> entries of several RSS feeds into one RSS document.

    Feeds that fail to parse contribute no items rather than aborting the merge,
    so a single misbehaving source cannot starve the pump of names from the
    others. Item and child-element namespaces are preserved in the merged XML.
    """
    channel = ElementTree.Element("channel")
    for feed_xml in feed_xmls:
        try:
            root = ElementTree.fromstring(feed_xml)
        except ElementTree.ParseError:
            continue
        for item in news_feed_items(root):
            channel.append(item)
    rss = ElementTree.Element("rss")
    rss.append(channel)
    return ElementTree.tostring(rss, encoding="unicode")


def fetch_news_feeds(
    urls: Iterable[str] = DEFAULT_NEWS_FEED_URLS,
    fetcher: Callable[[str], str] = fetch_news_feed,
    feed_observer: Callable[[str, str], None] | None = None,
) -> str:
    """Fetch several news feeds and return their combined RSS document.

    Each feed is fetched independently; an unreachable or erroring source is
    logged and skipped so the remaining sources still yield names.
    """
    urls = tuple(urls)
    feed_xmls = []
    for url in urls:
        try:
            feed_xml = fetcher(url)
        except requests.RequestException as exc:
            LOGGER.warning("News pump failed to fetch feed %s: %s", url, exc)
            continue
        feed_xmls.append(feed_xml)
        if feed_observer is not None:
            feed_observer(url, feed_xml)
    LOGGER.info("News pump fetched %d of %d news feed(s)", len(feed_xmls), len(urls))
    return combine_news_feeds(feed_xmls)


class NewsDescriptionParser(HTMLParser):
    """Extract article titles without retaining Google News publisher labels."""

    def __init__(self) -> None:
        super().__init__()
        self._anchor_depth = 0
        self._anchor_text: list[str] = []
        self.headlines: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._anchor_depth += 1
            if self._anchor_depth == 1:
                self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_depth:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._anchor_depth:
            return
        self._anchor_depth -= 1
        if self._anchor_depth == 0:
            headline = " ".join("".join(self._anchor_text).split())
            if headline:
                self.headlines.append(headline)


def news_item_headlines(feed_xml: str) -> list[str]:
    try:
        root = ElementTree.fromstring(feed_xml)
    except ElementTree.ParseError:
        return []

    headlines = []
    seen = set()
    for item in news_feed_items(root):
        title = " ".join(child_text(item, "title").split())
        source = " ".join(child_text(item, "source").split())
        source_suffix = f" - {source}"
        if source and title.endswith(source_suffix):
            title = title[: -len(source_suffix)].rstrip()

        item_headlines = [title] if title else []
        description = child_text(item, "description")
        if description:
            parser = NewsDescriptionParser()
            parser.feed(unescape(description))
            item_headlines.extend(parser.headlines)

        for headline in item_headlines:
            if headline not in seen:
                seen.add(headline)
                headlines.append(headline)
    return headlines


def normalize_person_key(value: str) -> str:
    parts = re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)
    normalized = []
    initials = []
    for part in parts:
        if len(part) == 1 and part.isalpha():
            initials.append(part)
            continue
        if initials:
            normalized.append("".join(initials))
            initials = []
        normalized.append(part)
    if initials:
        normalized.append("".join(initials))
    return " ".join(normalized)


@lru_cache(maxsize=1)
def load_person_index() -> dict[str, str]:
    with gzip.open(PERSON_INDEX_PATH, "rt", encoding="utf-8") as index_file:
        return json.load(index_file)


def names_in_headline(headline: str, person_index: dict[str, str]) -> list[str]:
    tokens = NEWS_TOKEN_PATTERN.findall(headline)
    names = []
    token_index = 0
    while token_index < len(tokens):
        max_width = min(MAX_PERSON_NAME_TOKENS, len(tokens) - token_index)
        for width in range(max_width, 0, -1):
            candidate = " ".join(tokens[token_index : token_index + width])
            canonical_name = person_index.get(normalize_person_key(candidate))
            if canonical_name:
                names.append(canonical_name)
                token_index += width
                break
        else:
            token_index += 1
    return names


def detect_proper_names(feed_xml: str, person_index: dict[str, str] | None = None) -> list[str]:
    """Return every person-name detection in a feed, including repeats."""
    index = person_index if person_index is not None else load_person_index()
    return [
        name
        for headline in news_item_headlines(feed_xml)
        for name in names_in_headline(headline, index)
    ]


def extract_proper_names(feed_xml: str, person_index: dict[str, str] | None = None) -> list[str]:
    names = []
    seen = set()
    for name in detect_proper_names(feed_xml, person_index):
        key = normalize_person_key(name)
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


class NewsPump:
    """Fetch names from a news RSS feed and slowly submit them for judgment."""

    def __init__(
        self,
        submit: Callable[[str, str], None],
        fetcher: Callable[[], str] | None = None,
        mean_seconds: float = NEWS_PUMP_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ):
        if mean_seconds <= 0:
            raise ValueError("mean_seconds must be positive")
        self._submit = submit
        self._fetcher = fetcher or fetch_news_feeds
        self._mean_seconds = mean_seconds
        self._sleep = sleeper
        self._random = random_source or random.Random()
        self._queue: deque[str] = deque()
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="news-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def queue_snapshot(self) -> list[str]:
        with self._lock:
            return list(self._queue)

    def seen_snapshot(self) -> set[str]:
        with self._lock:
            return set(self._seen)

    def refill_if_empty(self) -> int:
        with self._lock:
            if self._queue:
                LOGGER.debug(
                    "News pump refill skipped: queue already has %d name(s)", len(self._queue)
                )
                return 0
        LOGGER.info(
            "News pump fetching news feeds from %d source(s) across the US, UK, EU, and India",
            len(DEFAULT_NEWS_FEED_URLS),
        )
        fetch_started = time.monotonic()
        feed_xml = self._fetcher()
        fetch_seconds = time.monotonic() - fetch_started
        headlines = news_item_headlines(feed_xml)
        extracted = extract_proper_names(feed_xml)
        LOGGER.info(
            "News pump feed fetched: %d bytes in %.2fs, %d headline(s), %d distinct name(s) extracted",
            len(feed_xml),
            fetch_seconds,
            len(headlines),
            len(extracted),
        )
        added = 0
        skipped_seen = 0
        with self._lock:
            if self._queue:
                LOGGER.debug(
                    "News pump refill raced: queue refilled to %d name(s) by another pass",
                    len(self._queue),
                )
                return 0
            for name in extracted:
                if name not in self._seen:
                    self._seen.add(name)
                    self._queue.append(name)
                    added += 1
                else:
                    skipped_seen += 1
            queue_size = len(self._queue)
        LOGGER.info(
            "News pump refilled queue: %d new name(s) queued, %d already-seen name(s) skipped, "
            "queue now holds %d name(s) (%d name(s) seen all-time)",
            added,
            skipped_seen,
            queue_size,
            len(self._seen),
        )
        if added == 0:
            LOGGER.warning(
                "News pump added no new names; every extracted name was already seen. "
                "The pump will keep re-fetching every %.0fs until the feed changes.",
                NEWS_PUMP_FETCH_BACKOFF_SECONDS,
            )
        return added

    def next_delay(self) -> float:
        return self._random.expovariate(1.0 / self._mean_seconds)

    def pop_name(self) -> str | None:
        with self._lock:
            if not self._queue:
                return None
            name = self._queue.popleft()
            LOGGER.debug("News pump popped %r, %d name(s) left in queue", name, len(self._queue))
            return name

    def requeue(self, name: str) -> bool:
        """Put a rejected name back at the end of the queue for a later attempt."""
        with self._lock:
            if name in self._queue:
                return False
            self._queue.append(name)
            queue_size = len(self._queue)
        LOGGER.info("News pump requeued %r, %d name(s) in queue", name, queue_size)
        return True

    def run_once(self) -> bool:
        if not self.queue_snapshot():
            self.refill_if_empty()
        name = self.pop_name()
        if name is None:
            LOGGER.info(
                "News pump queue is empty; backing off for %.0fs before re-fetching the feed",
                NEWS_PUMP_FETCH_BACKOFF_SECONDS,
            )
            self._sleep(NEWS_PUMP_FETCH_BACKOFF_SECONDS)
            return False
        delay = self.next_delay()
        LOGGER.info(
            "News pump waiting %.1fs (mean %.0fs) before submitting next name %r",
            delay,
            self._mean_seconds,
            name,
        )
        self._sleep(delay)
        request_id = f"news-pump:{uuid.uuid4()}"
        LOGGER.info("News pump sending fetched name to grid: request_id=%s name=%s", request_id, name)
        submit_started = time.monotonic()
        self._submit(request_id, name)
        LOGGER.info(
            "News pump submitted name to grid: request_id=%s name=%s in %.2fs",
            request_id,
            name,
            time.monotonic() - submit_started,
        )
        return True

    def run(self) -> None:
        LOGGER.info(
            "News pump thread started: mean %.0fs between submissions (~%.1f submissions/min), "
            "%.0fs fetch backoff",
            self._mean_seconds,
            60.0 / self._mean_seconds if self._mean_seconds else 0.0,
            NEWS_PUMP_FETCH_BACKOFF_SECONDS,
        )
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                LOGGER.warning(
                    "News pump iteration failed: %s: %s", type(exc).__name__, exc, exc_info=True
                )
                self._sleep(NEWS_PUMP_FETCH_BACKOFF_SECONDS)
        LOGGER.info("News pump thread stopped")


class HistoryStore:
    """Single-process, append-only JSON Lines result history."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._results: list[dict] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as history_file:
                for line_number, line in enumerate(history_file, 1):
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Invalid history at line {line_number}") from exc
                    if not isinstance(record, dict) or record.get("sequence") != line_number:
                        raise ValueError(f"Invalid history sequence at line {line_number}")
                    self._results.append(record)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._results)

    def lookup_cached(self, subject: str, enable_purgatory: bool) -> dict | None:
        """Return an exact cached result without rewriting or reordering history."""
        with self._lock:
            result = next(
                (
                    result
                    for result in reversed(self._results)
                    if result.get("subject") == subject
                    and self._result_uses_purgatory(result) == enable_purgatory
                ),
                None,
            )
            return dict(result) if result is not None else None

    def _result_uses_purgatory(self, result: dict) -> bool:
        enable_purgatory = result.get("enable_purgatory")
        if isinstance(enable_purgatory, bool):
            return enable_purgatory
        probabilities = result.get("probabilities")
        return isinstance(probabilities, dict) and "purgatory" in probabilities

    def _write_all_locked(self, results: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4()}.tmp")
        try:
            with temporary_path.open("wb") as history_file:
                for result in results:
                    line = (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                    history_file.write(line)
                history_file.flush()
                os.fsync(history_file.fileno())
            os.replace(temporary_path, self.path)
            try:
                directory = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def append(self, request_id: str, subject: str, evaluation: dict) -> dict:
        with self._lock:
            result = {
                "id": str(uuid.uuid4()),
                "request_id": request_id,
                "subject": subject,
                "enable_purgatory": "purgatory" in evaluation["probabilities"],
                "choice": evaluation["choice"],
                "probabilities": evaluation["probabilities"],
                "confidence": evaluation["confidence"],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "sequence": len(self._results) + 1,
            }
            result.update(telemetry_from_evaluation(evaluation))
            line = (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as history_file:
                history_file.seek(0, os.SEEK_END)
                start = history_file.tell()
                try:
                    history_file.write(line)
                    history_file.flush()
                    os.fsync(history_file.fileno())
                except OSError:
                    history_file.truncate(start)
                    history_file.flush()
                    raise
            self._results.append(result)
            return result

    def promote_cached(self, request_id: str, subject: str, enable_purgatory: bool) -> dict | None:
        with self._lock:
            match_index = next(
                (
                    index
                    for index, result in enumerate(self._results)
                    if result.get("subject") == subject and self._result_uses_purgatory(result) == enable_purgatory
                ),
                None,
            )
            if match_index is None:
                return None

            cached = self._results[match_index]
            new_results = [dict(result) for index, result in enumerate(self._results) if index != match_index]
            for sequence, result in enumerate(new_results, 1):
                result["sequence"] = sequence

            promoted = {
                "id": str(uuid.uuid4()),
                "request_id": request_id,
                "subject": cached["subject"],
                "enable_purgatory": enable_purgatory,
                "choice": cached["choice"],
                "probabilities": cached["probabilities"],
                "confidence": cached["confidence"],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "sequence": len(new_results) + 1,
            }
            promoted.update(telemetry_from_evaluation(cached))
            new_results.append(promoted)
            self._write_all_locked(new_results)
            self._results = new_results
            return promoted


class IpRateLimiter:
    """In-memory sliding-window rate limiter keyed by client IP."""

    MINUTE_SECONDS = 60.0
    DAY_SECONDS = 24 * 60 * 60.0

    def __init__(
        self,
        per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        per_day: int = DEFAULT_RATE_LIMIT_PER_DAY,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_ips: int = MAX_TRACKED_RATE_LIMIT_IPS,
    ):
        if per_minute < 0 or per_day < 0:
            raise ValueError("Rate limits must be zero or greater")
        if max_tracked_ips <= 0:
            raise ValueError("Tracked IP limit must be positive")
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._max_tracked_ips = max_tracked_ips
        self._last_sweep = 0.0

    def allow(self, ip_address: str) -> bool:
        if self.per_minute == 0 and self.per_day == 0:
            return True

        now = self._clock()
        cutoff = now - self.DAY_SECONDS
        with self._lock:
            if now - self._last_sweep >= self.MINUTE_SECONDS:
                for tracked_ip, tracked_hits in list(self._hits.items()):
                    while tracked_hits and tracked_hits[0] <= cutoff:
                        tracked_hits.popleft()
                    if not tracked_hits:
                        del self._hits[tracked_ip]
                self._last_sweep = now

            hits = self._hits.get(ip_address)
            if hits is None:
                if len(self._hits) >= self._max_tracked_ips:
                    return False
                hits = self._hits[ip_address] = deque()
            while hits and hits[0] <= cutoff:
                hits.popleft()

            minute_start = now - self.MINUTE_SECONDS
            minute_count = 0
            for timestamp in reversed(hits):
                if timestamp <= minute_start:
                    break
                minute_count += 1

            if self.per_minute and minute_count >= self.per_minute:
                return False
            if self.per_day and len(hits) >= self.per_day:
                return False

            hits.append(now)
            return True


def client_ip() -> str:
    if os.environ.get("FLY_APP_NAME"):
        fly_client_ip = request.headers.get("Fly-Client-IP")
        if fly_client_ip:
            return fly_client_ip
    return request.remote_addr or "unknown"


def create_app(
    history_file: str | Path | None = None,
    evaluator: Callable[..., dict] | None = None,
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    rate_limit_per_day: int = DEFAULT_RATE_LIMIT_PER_DAY,
    use_cache: bool = True,
    log_api_calls: bool = False,
    news_pump_enabled: bool | None = None,
    news_fetcher: Callable[[], str] | None = None,
    news_pump_interval: float | None = None,
    news_pump_mean_seconds: float | None = None,
    max_concurrent_evaluations: int = MAX_CONCURRENT_EVALUATIONS,
    max_queued_evaluations: int = MAX_QUEUED_EVALUATIONS,
    max_queue_wait_seconds: float = MAX_QUEUE_WAIT_SECONDS,
    max_outstanding_evaluations_per_ip: int = MAX_OUTSTANDING_EVALUATIONS_PER_IP,
    max_subscribers_per_evaluation: int = MAX_SUBSCRIBERS_PER_EVALUATION,
) -> tuple[Flask, SocketIO]:
    if min(
        max_concurrent_evaluations,
        max_queued_evaluations,
        max_outstanding_evaluations_per_ip,
        max_subscribers_per_evaluation,
    ) <= 0 or max_queue_wait_seconds <= 0:
        raise ValueError("Evaluation queue limits must be positive")
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    if log_api_calls:
        LOGGER.setLevel(logging.INFO)
        if not LOGGER.handlers and not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO)
    socketio = SocketIO(app, async_mode="threading")
    store = HistoryStore(
        Path(history_file or os.environ.get("HISTORY_FILE") or Path(__file__).parent / "data" / "history.jsonl")
    )
    def default_evaluator(subject: str, enable_purgatory: bool = True) -> dict:
        return evaluate_subject(subject, enable_purgatory, log_api_calls=log_api_calls)

    evaluate = evaluator or default_evaluator
    evaluate_with_toggle = accepts_enable_purgatory(evaluate)
    limiter = IpRateLimiter(rate_limit_per_minute, rate_limit_per_day)
    pending: set[tuple[str, str]] = set()
    pending_by_sid: dict[str, str] = {}
    recent_responses: OrderedDict[tuple[str, str], tuple[str, dict]] = OrderedDict()
    pending_lock = threading.Lock()
    evaluation_queue: deque[dict] = deque()
    jobs_by_key: dict[tuple[str, bool], dict] = {}
    outstanding_by_ip: dict[str, int] = {}
    queue_lock = threading.Lock()
    running_evaluations = 0
    connected_clients: set[str] = set()
    connected_clients_lock = threading.Lock()
    news_pump_enable_purgatory = False
    news_pump_preference_lock = threading.Lock()

    def set_news_pump_purgatory_preference(enable_purgatory: bool) -> None:
        nonlocal news_pump_enable_purgatory
        with news_pump_preference_lock:
            news_pump_enable_purgatory = enable_purgatory

    def get_news_pump_purgatory_preference() -> bool:
        with news_pump_preference_lock:
            return news_pump_enable_purgatory

    def broadcast_connection_count() -> None:
        with connected_clients_lock:
            count = len(connected_clients)
        socketio.emit("connection:count", {"count": count})

    def cached_result_for(request_id: str, subject: str, enable_purgatory: bool) -> dict | None:
        if not use_cache:
            return None
        result = store.lookup_cached(subject, enable_purgatory)
        if result is not None:
            app.logger.info(
                "History cache hit substituted for TypeSafe API call: request_id=%s enable_purgatory=%s result_id=%s",
                request_id,
                enable_purgatory,
                result["id"],
            )
        return result

    def finish_pending(subscriber: dict, event_name: str | None = None, payload: dict | None = None) -> None:
        sid = subscriber.get("sid")
        if sid is None:
            return
        key = (sid, subscriber["request_id"])
        with pending_lock:
            pending.discard(key)
            if pending_by_sid.get(sid) == subscriber["request_id"]:
                del pending_by_sid[sid]
            if event_name is None or payload is None:
                return
            recent_responses[key] = (event_name, payload)
            recent_responses.move_to_end(key)
            while len(recent_responses) > MAX_RECENT_REQUEST_RESPONSES:
                recent_responses.popitem(last=False)

    def notify_accepted(subscriber: dict, status: str, queue_position: int = 0) -> None:
        sid = subscriber.get("sid")
        if sid is not None:
            socketio.emit(
                "judgment:accepted",
                {
                    "request_id": subscriber["request_id"],
                    "status": status,
                    "queue_position": queue_position,
                },
                to=sid,
            )

    def notify_complete(subscribers: list[dict], result: dict) -> None:
        for subscriber in subscribers:
            sid = subscriber.get("sid")
            if sid is not None:
                payload = {"request_id": subscriber["request_id"], "result_id": result["id"]}
                finish_pending(subscriber, "judgment:complete", payload)
                socketio.emit(
                    "judgment:complete",
                    payload,
                    to=sid,
                )

    def notify_error(subscribers: list[dict], message: str) -> None:
        for subscriber in subscribers:
            sid = subscriber.get("sid")
            if sid is not None:
                payload = {"request_id": subscriber["request_id"], "message": message}
                finish_pending(subscriber, "judgment:error", payload)
                socketio.emit(
                    "judgment:error",
                    payload,
                    to=sid,
                )

    def detach_job_subscribers(job: dict) -> list[dict]:
        """Close a job to new subscribers and return a stable recipient list."""
        with queue_lock:
            if jobs_by_key.get(job["key"]) is job:
                del jobs_by_key[job["key"]]
            return list(job["subscribers"].values())

    def release_job(job: dict) -> None:
        nonlocal running_evaluations
        with queue_lock:
            if job["state"] == "running":
                running_evaluations -= 1
            if jobs_by_key.get(job["key"]) is job:
                del jobs_by_key[job["key"]]
            owner_ip = job.get("owner_ip")
            if owner_ip is not None:
                remaining = outstanding_by_ip.get(owner_ip, 1) - 1
                if remaining > 0:
                    outstanding_by_ip[owner_ip] = remaining
                else:
                    outstanding_by_ip.pop(owner_ip, None)

    def process_job(job: dict) -> None:
        subscribers = None
        try:
            evaluation = (
                evaluate(job["subject"], job["enable_purgatory"])
                if evaluate_with_toggle
                else evaluate(job["subject"])
            )
            result = store.append(job["request_id"], job["subject"], evaluation)
            subscribers = detach_job_subscribers(job)
            socketio.emit("judgment:result", result)
            notify_complete(subscribers, result)
        except JudgmentError as exc:
            if subscribers is None:
                subscribers = detach_job_subscribers(job)
            if not any(subscriber.get("sid") is not None for subscriber in subscribers):
                app.logger.info("News pump judgment skipped: %s", str(exc))
            notify_error(subscribers, str(exc))
        except Exception as exc:
            if subscribers is None:
                subscribers = detach_job_subscribers(job)
            app.logger.error("Judgment failed: %s", type(exc).__name__)
            notify_error(subscribers, "The judgment failed. Please try again.")
        finally:
            release_job(job)
            dispatch_jobs()

    def dispatch_jobs() -> None:
        nonlocal running_evaluations
        expired = []
        to_start = []
        now = time.monotonic()
        with queue_lock:
            while evaluation_queue and running_evaluations < max_concurrent_evaluations:
                job = evaluation_queue.popleft()
                if job["deadline"] <= now:
                    if jobs_by_key.get(job["key"]) is job:
                        del jobs_by_key[job["key"]]
                    owner_ip = job.get("owner_ip")
                    if owner_ip is not None:
                        remaining = outstanding_by_ip.get(owner_ip, 1) - 1
                        if remaining > 0:
                            outstanding_by_ip[owner_ip] = remaining
                        else:
                            outstanding_by_ip.pop(owner_ip, None)
                    expired.append(job)
                    continue
                job["state"] = "running"
                running_evaluations += 1
                to_start.append(job)

        for job in expired:
            notify_error(list(job["subscribers"].values()), "The site stayed busy too long. Please try again.")
        for job in to_start:
            try:
                socketio.start_background_task(process_job, job)
            except Exception:
                subscribers = detach_job_subscribers(job)
                notify_error(subscribers, "The site could not start the judgment. Please try again.")
                release_job(job)
                dispatch_jobs()

    def enqueue_job(
        request_id: str,
        subject: str,
        enable_purgatory: bool,
        subscriber: dict,
        owner_ip: str | None,
    ) -> tuple[str, int]:
        key = (subject, enable_purgatory)
        with queue_lock:
            existing = jobs_by_key.get(key)
            if existing is not None:
                if len(existing["subscribers"]) >= max_subscribers_per_evaluation:
                    return "subscriber_full", 0
                existing["subscribers"][(subscriber.get("sid"), request_id)] = subscriber
                return "coalesced", 0

            if owner_ip is not None and outstanding_by_ip.get(owner_ip, 0) >= max_outstanding_evaluations_per_ip:
                return "ip_full", 0
            if len(evaluation_queue) >= max_queued_evaluations:
                return "queue_full", 0

            job = {
                "key": key,
                "request_id": request_id,
                "subject": subject,
                "enable_purgatory": enable_purgatory,
                "owner_ip": owner_ip,
                "enqueued_at": time.monotonic(),
                "deadline": time.monotonic() + max_queue_wait_seconds,
                "state": "queued",
                "subscribers": {(subscriber.get("sid"), request_id): subscriber},
            }
            jobs_by_key[key] = job
            evaluation_queue.append(job)
            if owner_ip is not None:
                outstanding_by_ip[owner_ip] = outstanding_by_ip.get(owner_ip, 0) + 1
            queue_position = len(evaluation_queue)

        return "queued", queue_position

    def submit_news_subject(request_id: str, subject: str) -> None:
        enable_purgatory = get_news_pump_purgatory_preference()
        result = cached_result_for(request_id, subject, enable_purgatory)
        if result is not None:
            socketio.emit("judgment:result", result)
            return
        status, _ = enqueue_job(
            request_id,
            subject,
            enable_purgatory,
            {"sid": None, "request_id": request_id, "ip": None},
            None,
        )
        if status not in {"queued", "coalesced"}:
            raise JudgmentError("The site is busy. Please try again shortly.")
        if status == "queued":
            dispatch_jobs()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        if not os.environ.get("TYPESAFE_API_KEY"):
            return {"status": "unconfigured"}, 503
        return {"status": "ok"}, 200

    @socketio.on("connect")
    def on_connect():
        with connected_clients_lock:
            connected_clients.add(request.sid)
        emit("judgment:history", {"results": store.snapshot()})
        broadcast_connection_count()

    @socketio.on("disconnect")
    def on_disconnect():
        with pending_lock:
            request_id = pending_by_sid.pop(request.sid, None)
            if request_id is not None:
                pending.discard((request.sid, request_id))
        with connected_clients_lock:
            connected_clients.discard(request.sid)
        broadcast_connection_count()

    @socketio.on("purgatory:preference")
    def on_purgatory_preference(payload):
        enable_purgatory = payload.get("enable_purgatory") if isinstance(payload, dict) else None
        if isinstance(enable_purgatory, bool):
            set_news_pump_purgatory_preference(enable_purgatory)

    @socketio.on("judgment:submit")
    def on_submit(payload):
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        subject = payload.get("subject") if isinstance(payload, dict) else None
        enable_purgatory = payload.get("enable_purgatory", False) if isinstance(payload, dict) else False
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 100
            or not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > MAX_SUBJECT_LENGTH
            or not isinstance(enable_purgatory, bool)
        ):
            emit("judgment:error", {"request_id": request_id if isinstance(request_id, str) else "", "message": "Enter a name or concept of up to 200 characters."})
            return
        set_news_pump_purgatory_preference(enable_purgatory)

        sid = request.sid
        key = (sid, request_id)
        replay = None
        sid_busy = False
        with pending_lock:
            replay = recent_responses.get(key)
            if replay is not None:
                recent_responses.move_to_end(key)
            elif key in pending:
                return
            elif sid in pending_by_sid:
                sid_busy = True
            else:
                pending.add(key)
                pending_by_sid[sid] = request_id
        if replay is not None:
            event_name, response_payload = replay
            emit(event_name, response_payload)
            return

        ip_address = client_ip()
        subscriber = {"sid": sid, "request_id": request_id, "ip": ip_address}
        if not limiter.allow(ip_address):
            notify_error([subscriber], "Rate limit reached. Please try again later.")
            return
        if sid_busy:
            notify_error([subscriber], "Wait for the current judgment to finish.")
            return

        result = cached_result_for(request_id, subject, enable_purgatory)
        if result is not None:
            notify_accepted(subscriber, "cache_hit")
            socketio.emit("judgment:result", result)
            notify_complete([subscriber], result)
            return

        status, queue_position = enqueue_job(request_id, subject, enable_purgatory, subscriber, ip_address)
        if status in {"queued", "coalesced"}:
            notify_accepted(subscriber, status, queue_position)
            if status == "queued":
                dispatch_jobs()
            return

        if status == "ip_full":
            message = "Too many judgments are already pending from this address. Please wait."
        elif status == "subscriber_full":
            message = "Too many people are waiting for this judgment. Please try again shortly."
        else:
            message = "The site is busy. Please try again shortly."
        notify_error([subscriber], message)

    news_pump = None
    should_start_news_pump = news_pump_enabled if news_pump_enabled is not None else env_flag("NEWS_PUMP_ENABLED")
    if should_start_news_pump:
        pump_interval = news_pump_mean_seconds
        if pump_interval is None:
            pump_interval = news_pump_interval
        if pump_interval is None:
            pump_interval = env_positive_float("NEWS_PUMP_INTERVAL_SECONDS", NEWS_PUMP_INTERVAL_SECONDS)
        news_pump = NewsPump(submit_news_subject, fetcher=news_fetcher, mean_seconds=pump_interval)
        news_pump.start()

    app.extensions["history_store"] = store
    app.extensions["ip_rate_limiter"] = limiter
    app.extensions["evaluation_queue"] = evaluation_queue
    app.extensions["jobs_by_key"] = jobs_by_key
    app.extensions["use_history_cache"] = use_cache
    app.extensions["submit_news_subject"] = submit_news_subject
    app.extensions["news_pump"] = news_pump
    return app, socketio


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a nonnegative integer") from exc
    if parsed < 0:
        raise ArgumentTypeError("must be a nonnegative integer")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ArgumentTypeError("must be a positive number")
    return parsed


def parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(description="Run the Heaven or Hell server.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind to, such as 0.0.0.0 for all interfaces (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--rate-limit-per-minute",
        type=nonnegative_int,
        default=DEFAULT_RATE_LIMIT_PER_MINUTE,
        help="accepted submissions per IP per minute; 0 disables this limit (default: 1)",
    )
    parser.add_argument(
        "--rate-limit-per-day",
        type=nonnegative_int,
        default=DEFAULT_RATE_LIMIT_PER_DAY,
        help="accepted submissions per IP per rolling 24 hours; 0 disables this limit (default: 100)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="disable history cache hits and call TypeSafe for every accepted submission",
    )
    parser.add_argument(
        "--log-api-calls",
        action="store_true",
        help="pretty-print TypeSafe API requests and responses to the server log with the API key redacted",
    )
    parser.add_argument(
        "--news-pump",
        action="store_true",
        help="fetch Google News RSS names and submit them in the background",
    )
    parser.add_argument(
        "--news-pump-interval",
        "--news-pump-mean-seconds",
        type=positive_float,
        dest="news_pump_interval",
        default=env_positive_float("NEWS_PUMP_INTERVAL_SECONDS", NEWS_PUMP_INTERVAL_SECONDS),
        help="mean seconds between background news submissions (default: 300)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    application, server = create_app(
        rate_limit_per_minute=args.rate_limit_per_minute,
        rate_limit_per_day=args.rate_limit_per_day,
        use_cache=not args.no_cache,
        log_api_calls=args.log_api_calls,
        news_pump_enabled=args.news_pump or env_flag("NEWS_PUMP_ENABLED"),
        news_pump_interval=args.news_pump_interval,
    )
    server.run(application, host=args.host, port=int(os.environ.get("PORT", "5077")), allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
