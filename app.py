"""Flask and Socket.IO server for the Heaven or Hell site."""

from __future__ import annotations

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
from collections import deque
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from collections.abc import Callable
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
DEFAULT_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_RATE_LIMIT_PER_DAY = 500
DEFAULT_NEWS_FEED_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
NEWS_PUMP_MEAN_SECONDS = 15.0
NEWS_PUMP_FETCH_BACKOFF_SECONDS = 60.0
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
LOGGER = logging.getLogger(__name__)

NAME_TOKEN = r"(?:[A-Z][a-z]+|[A-Z]\.|[A-Z]{2,})"
NAME_PATTERN = re.compile(
    rf"\b{NAME_TOKEN}(?:(?:\s+(?:de|del|der|di|la|le|van|von|bin|al|of|the|[A-Z]'[A-Z][a-z]+|{NAME_TOKEN}))){{1,4}}\b"
)
NEWS_NAME_STOPWORDS = {
    "Associated Press",
    "Breaking News",
    "CBS News",
    "Fox News",
    "Google News",
    "NBC News",
    "New York",
    "Reuters",
    "The Associated Press",
    "The Guardian",
    "The Hill",
    "United Kingdom",
    "United States",
    "Wall Street",
    "Washington Post",
}
NEWS_NAME_PREFIXES = {
    "actor",
    "ceo",
    "chief",
    "coach",
    "dr",
    "king",
    "mr",
    "mrs",
    "ms",
    "president",
    "prime",
    "professor",
    "queen",
    "rep",
    "sen",
    "singer",
}


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


def fetch_news_feed(url: str = DEFAULT_NEWS_FEED_URL) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": "hotornot-news-pump/1.0"},
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def news_item_text(feed_xml: str) -> list[str]:
    try:
        root = ElementTree.fromstring(feed_xml)
    except ElementTree.ParseError:
        return []

    texts = []
    for item in root.findall(".//item"):
        for tag in ("title", "description"):
            element = item.find(tag)
            if element is not None and element.text:
                texts.append(element.text)
    return texts


def normalize_news_text(text: str) -> str:
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def is_likely_person_name(name: str) -> bool:
    if name in NEWS_NAME_STOPWORDS:
        return False
    tokens = name.split()
    if not (2 <= len(tokens) <= 5):
        return False
    if tokens[0].lower() in {"the", "a", "an"}:
        return False
    if any(token.lower() in {"news", "live", "video", "photos", "update", "updates"} for token in tokens):
        return False
    return any(token[:1].isupper() and token[1:].islower() for token in tokens)


def clean_news_name(name: str) -> str:
    tokens = name.split()
    while len(tokens) > 2 and tokens[0].rstrip(".").lower() in NEWS_NAME_PREFIXES:
        tokens = tokens[1:]
    return " ".join(tokens)


def extract_proper_names(feed_xml: str) -> list[str]:
    names = []
    seen = set()
    for text in news_item_text(feed_xml):
        for match in NAME_PATTERN.finditer(normalize_news_text(text)):
            name = clean_news_name(match.group(0).strip(" -:,."))
            if name and name not in seen and is_likely_person_name(name):
                seen.add(name)
                names.append(name)
    return names


class NewsPump:
    """Fetch names from a news RSS feed and slowly submit them for judgment."""

    def __init__(
        self,
        submit: Callable[[str, str], None],
        fetcher: Callable[[], str] | None = None,
        mean_seconds: float = NEWS_PUMP_MEAN_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ):
        if mean_seconds <= 0:
            raise ValueError("mean_seconds must be positive")
        self._submit = submit
        self._fetcher = fetcher or fetch_news_feed
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

    def refill_if_empty(self) -> int:
        with self._lock:
            if self._queue:
                return 0
        feed_xml = self._fetcher()
        added = 0
        with self._lock:
            if self._queue:
                return 0
            for name in extract_proper_names(feed_xml):
                if name not in self._seen:
                    self._seen.add(name)
                    self._queue.append(name)
                    added += 1
        return added

    def next_delay(self) -> float:
        return self._random.expovariate(1.0 / self._mean_seconds)

    def pop_name(self) -> str | None:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    def run_once(self) -> bool:
        if not self.queue_snapshot():
            self.refill_if_empty()
        name = self.pop_name()
        if name is None:
            self._sleep(NEWS_PUMP_FETCH_BACKOFF_SECONDS)
            return False
        self._sleep(self.next_delay())
        self._submit(f"news-pump:{uuid.uuid4()}", name)
        return True

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                LOGGER.warning("News pump iteration failed: %s", type(exc).__name__)
                self._sleep(NEWS_PUMP_FETCH_BACKOFF_SECONDS)


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
    ):
        if per_minute < 0 or per_day < 0:
            raise ValueError("Rate limits must be zero or greater")
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, ip_address: str) -> bool:
        if self.per_minute == 0 and self.per_day == 0:
            return True

        now = self._clock()
        cutoff = now - self.DAY_SECONDS
        with self._lock:
            hits = self._hits.setdefault(ip_address, deque())
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
    news_pump_mean_seconds: float = NEWS_PUMP_MEAN_SECONDS,
) -> tuple[Flask, SocketIO]:
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
    slots = threading.BoundedSemaphore(MAX_CONCURRENT_EVALUATIONS)
    limiter = IpRateLimiter(rate_limit_per_minute, rate_limit_per_day)
    pending: set[tuple[str, str]] = set()
    pending_lock = threading.Lock()

    def cached_result_for(request_id: str, subject: str, enable_purgatory: bool) -> dict | None:
        if not use_cache:
            return None
        try:
            result = store.promote_cached(request_id, subject, enable_purgatory)
        except Exception as exc:
            app.logger.error("History cache promotion failed: %s", type(exc).__name__)
            return None
        if result is not None:
            app.logger.info(
                "History cache hit substituted for TypeSafe API call: request_id=%s enable_purgatory=%s result_id=%s",
                request_id,
                enable_purgatory,
                result["id"],
            )
        return result

    def evaluate_and_broadcast(request_id: str, subject: str, enable_purgatory: bool, sid: str | None = None) -> None:
        try:
            result = cached_result_for(request_id, subject, enable_purgatory)
            cache_hit = result is not None
            if result is None:
                evaluation = evaluate(subject, enable_purgatory) if evaluate_with_toggle else evaluate(subject)
                result = store.append(request_id, subject, evaluation)
            socketio.emit("judgment:result", result)
            if cache_hit:
                socketio.emit("judgment:history", {"results": store.snapshot()})
        except JudgmentError as exc:
            if sid is None:
                app.logger.info("News pump judgment skipped: %s", str(exc))
            else:
                socketio.emit("judgment:error", {"request_id": request_id, "message": str(exc)}, to=sid)
        except Exception as exc:
            app.logger.error("Judgment failed: %s", type(exc).__name__)
            if sid is not None:
                socketio.emit(
                    "judgment:error",
                    {"request_id": request_id, "message": "The judgment failed. Please try again."},
                    to=sid,
                )

    def submit_news_subject(request_id: str, subject: str) -> None:
        slots.acquire()
        try:
            evaluate_and_broadcast(request_id, subject, True)
        finally:
            slots.release()

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
        emit("judgment:history", {"results": store.snapshot()})

    @socketio.on("judgment:submit")
    def on_submit(payload):
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        subject = payload.get("subject") if isinstance(payload, dict) else None
        enable_purgatory = payload.get("enable_purgatory", True) if isinstance(payload, dict) else True
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

        sid = request.sid
        key = (sid, request_id)
        with pending_lock:
            if key in pending:
                return
            pending.add(key)
        result = cached_result_for(request_id, subject, enable_purgatory)
        if result is not None:
            with pending_lock:
                pending.discard(key)
            socketio.emit("judgment:result", result)
            socketio.emit("judgment:history", {"results": store.snapshot()})
            return

        if not limiter.allow(client_ip()):
            with pending_lock:
                pending.discard(key)
            emit("judgment:error", {"request_id": request_id, "message": "Rate limit reached. Please try again later."})
            return
        if not slots.acquire(blocking=False):
            with pending_lock:
                pending.discard(key)
            emit("judgment:error", {"request_id": request_id, "message": "The site is busy. Please try again shortly."})
            return

        def process():
            try:
                evaluate_and_broadcast(request_id, subject, enable_purgatory, sid)
            finally:
                with pending_lock:
                    pending.discard(key)
                slots.release()

        try:
            socketio.start_background_task(process)
        except Exception:
            with pending_lock:
                pending.discard(key)
            slots.release()
            emit("judgment:error", {"request_id": request_id, "message": "The site could not start the judgment. Please try again."})

    news_pump = None
    should_start_news_pump = news_pump_enabled if news_pump_enabled is not None else env_flag("NEWS_PUMP_ENABLED")
    if should_start_news_pump:
        news_pump = NewsPump(submit_news_subject, fetcher=news_fetcher, mean_seconds=news_pump_mean_seconds)
        news_pump.start()

    app.extensions["history_store"] = store
    app.extensions["ip_rate_limiter"] = limiter
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
        "--news-pump-mean-seconds",
        type=positive_float,
        default=NEWS_PUMP_MEAN_SECONDS,
        help="mean seconds between background news submissions (default: 15)",
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
        news_pump_mean_seconds=args.news_pump_mean_seconds,
    )
    server.run(application, host=args.host, port=int(os.environ.get("PORT", "5077")), allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
