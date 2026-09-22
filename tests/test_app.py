import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app as game


ANSWER = {
    "choice": "purgatory",
    "probabilities": {"heaven": 0.2, "hell": 0.1, "purgatory": 0.7},
    "confidence": 0.58,
}


def wait_for_event(client, name, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in client.get_received():
            if event["name"] == name:
                return event["args"][0]
        time.sleep(0.01)
    pytest.fail(f"Did not receive {name}")


def test_typesafe_request_uses_unchanged_state_and_exact_choice(monkeypatch):
    seen = {}

    class Response:
        ok = True
        status_code = 200

        def json(self):
            return {"answers": {"destination": {"type": "choice", **ANSWER}}}

    def fake_post(url, *, json, headers, timeout):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    monkeypatch.setattr(game.requests, "post", fake_post)
    subject = "  Ada Lovelace  "
    assert game.evaluate_subject(subject) == ANSWER
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


def test_page_serves_html_without_application_rest_api(tmp_path):
    app, _ = game.create_app(tmp_path / "history.jsonl", evaluator=lambda subject: ANSWER)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Heaven" in response.data
    assert client.get("/api/results").status_code == 404
