"""Flask and Socket.IO server for the Heaven or Hell game."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
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
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


class JudgmentError(Exception):
    """A failure that can be reported safely to the submitting browser."""


def destinations_for(enable_purgatory: bool) -> tuple[str, ...]:
    return DESTINATIONS if enable_purgatory else CORE_DESTINATIONS


def evaluate_subject(subject: str, enable_purgatory: bool = True) -> dict:
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
        try:
            response = requests.post(TYPESAFE_URL, json=body, headers=headers, timeout=25)
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
            return validate_answer(response.json(), destinations)
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

    return {
        "choice": choice,
        "probabilities": {key: float(probabilities[key]) for key in destinations},
        "confidence": float(confidence),
    }


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

    def append(self, request_id: str, subject: str, evaluation: dict) -> dict:
        with self._lock:
            result = {
                "id": str(uuid.uuid4()),
                "request_id": request_id,
                "subject": subject,
                "choice": evaluation["choice"],
                "probabilities": evaluation["probabilities"],
                "confidence": evaluation["confidence"],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "sequence": len(self._results) + 1,
            }
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


def create_app(
    history_file: str | Path | None = None,
    evaluator: Callable[..., dict] | None = None,
) -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    socketio = SocketIO(app, async_mode="threading")
    store = HistoryStore(
        Path(history_file or os.environ.get("HISTORY_FILE") or Path(__file__).parent / "data" / "history.jsonl")
    )
    evaluate = evaluator or evaluate_subject
    evaluate_with_toggle = accepts_enable_purgatory(evaluate)
    slots = threading.BoundedSemaphore(MAX_CONCURRENT_EVALUATIONS)
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
    return app, socketio


if __name__ == "__main__":
    application, server = create_app()
    server.run(application, host="127.0.0.1", port=int(os.environ.get("PORT", "5077")), allow_unsafe_werkzeug=True)
