# Heaven or Hell

A shared Jev judgment game built with Flask, Socket.IO, and Vue. Enter a name or concept to see Jev's probabilities for Heaven, Hell, and Purgatory. Results are broadcast to every connected browser, shown newest first, and saved across restarts.

## Run locally

Requires Python 3.10+ and an internet connection for the TypeSafe API and the pinned Vue and Socket.IO browser scripts.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TYPESAFE_API_KEY='your-key-here'
python app.py
```

Open `http://127.0.0.1:5077` in one or more browser windows. Set `PORT` to use another port. Use the numeric `127.0.0.1` address: on some Macs, `localhost:5000` reaches AirPlay instead of Flask and returns HTTP 403. If an older instance is already running on port 5000, use `http://127.0.0.1:5000` until you restart it. The application uses Socket.IO for all game messages; HTTP serves only the page and static assets.

Successful results are stored at `data/history.jsonl`. Set `HISTORY_FILE` to an absolute path to use another location. To reset local history, stop the server and remove that file. Run one server process for this version; the file store is designed for one process.

The API key is read only on the server. Without it, the page still loads but submissions show a configuration error.

## Tests

```sh
python -m pytest -q
```

Tests use a fake TypeSafe response and do not spend API credits. A live smoke test requires a valid `TYPESAFE_API_KEY` and an actual submission through the page.

The product specification is in [docs/spec.md](docs/spec.md); the implementation plan is in [docs/build-plan.md](docs/build-plan.md).
