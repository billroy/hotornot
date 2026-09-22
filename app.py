"""Flask and Socket.IO server for the Heaven or Hell game."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from inspect import signature

import requests
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit


DESTINATIONS = ("heaven", "hell", "purgatory")
CORE_DESTINATIONS = ("heaven", "hell")
QUESTION_ID = "destination"
MAX_SUBJECT_LENGTH = 200
MAX_CONCURRENT_EVALUATIONS = 4
DEFAULT_RATE_LIMIT_PER_MINUTE = 1
DEFAULT_RATE_LIMIT_PER_DAY = 100
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
LOGGER = logging.getLogger(__name__)


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
        raise JudgmentError("The game is not configured with a TypeSafe API key.")

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
    return request.remote_addr or "unknown"


def create_app(
    history_file: str | Path | None = None,
    evaluator: Callable[..., dict] | None = None,
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    rate_limit_per_day: int = DEFAULT_RATE_LIMIT_PER_DAY,
    use_cache: bool = True,
    log_api_calls: bool = False,
) -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
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

    @app.get("/")
    def index():
        return render_template("index.html")

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
        if use_cache:
            try:
                result = store.promote_cached(request_id, subject, enable_purgatory)
            except Exception as exc:
                app.logger.error("History cache promotion failed: %s", type(exc).__name__)
                result = None
            if result is not None:
                app.logger.info(
                    "History cache hit substituted for TypeSafe API call: request_id=%s enable_purgatory=%s result_id=%s",
                    request_id,
                    enable_purgatory,
                    result["id"],
                )
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
            emit("judgment:error", {"request_id": request_id, "message": "The game is busy. Please try again shortly."})
            return

        def process():
            try:
                evaluation = evaluate(subject, enable_purgatory) if evaluate_with_toggle else evaluate(subject)
                result = store.append(request_id, subject, evaluation)
                socketio.emit("judgment:result", result)
            except JudgmentError as exc:
                socketio.emit("judgment:error", {"request_id": request_id, "message": str(exc)}, to=sid)
            except Exception as exc:
                app.logger.error("Judgment failed: %s", type(exc).__name__)
                socketio.emit("judgment:error", {"request_id": request_id, "message": "The judgment failed. Please try again."}, to=sid)
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
            emit("judgment:error", {"request_id": request_id, "message": "The game could not start the judgment. Please try again."})

    app.extensions["history_store"] = store
    app.extensions["ip_rate_limiter"] = limiter
    app.extensions["use_history_cache"] = use_cache
    return app, socketio


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a nonnegative integer") from exc
    if parsed < 0:
        raise ArgumentTypeError("must be a nonnegative integer")
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    application, server = create_app(
        rate_limit_per_minute=args.rate_limit_per_minute,
        rate_limit_per_day=args.rate_limit_per_day,
        use_cache=not args.no_cache,
        log_api_calls=args.log_api_calls,
    )
    server.run(application, host=args.host, port=int(os.environ.get("PORT", "5077")), allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
