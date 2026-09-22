# Heaven or Hell

A shared Jev judgment site built with Flask, Socket.IO, and Vue. Enter a name or concept to see Jev's probabilities for Heaven, Hell, and Purgatory. Results are broadcast to every connected browser, shown newest first, and saved across restarts.

## Run locally

Requires Python 3.10+ and an internet connection for the TypeSafe API and the pinned Vue and Socket.IO browser scripts.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TYPESAFE_API_KEY='your-key-here'
python app.py
```

Open `http://127.0.0.1:5077` in one or more browser windows. Set `PORT` to use another port. Use `python app.py --host 0.0.0.0` to bind to all interfaces for access from other devices on your network. Submissions are rate limited per IP to 10 per minute and 500 per rolling 24 hours by default; change these with `--rate-limit-per-minute` and `--rate-limit-per-day`, or set either value to `0` to disable that specific limit. Exact repeats of a previous subject with the same Purgatory toggle use the saved history instead of calling TypeSafe, promote that result to the top of history, and broadcast the updated history to connected browsers. Add `--no-cache` to call TypeSafe for every accepted submission. Use the numeric `127.0.0.1` address: on some Macs, `localhost:5000` reaches AirPlay instead of Flask and returns HTTP 403. If an older instance is already running on port 5000, use `http://127.0.0.1:5000` until you restart it. The application uses Socket.IO for all messages; HTTP serves only the page and static assets.

Successful results are stored at `data/history.jsonl`. Set `HISTORY_FILE` to an absolute path to use another location. To reset local history, stop the server and remove that file. Run one server process for this version; the file store is designed for one process.

The API key is read only on the server. Without it, the page still loads but submissions show a configuration error.

## Tests

```sh
python -m pytest -q
```

Tests use a fake TypeSafe response and do not spend API credits. A live smoke test requires a valid `TYPESAFE_API_KEY` and an actual submission through the page.

The product specification is in [docs/spec.md](docs/spec.md); the implementation plan is in [docs/build-plan.md](docs/build-plan.md).
