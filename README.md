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

Add `--news-pump` or set `NEWS_PUMP_ENABLED=1` to fetch names from news RSS in the background. It draws on a range of responsible mainstream sources across the US, UK, and EU (Google News regional editions plus outlets such as NPR, PBS, the BBC, The Guardian, France 24, and Deutsche Welle); the feeds are fetched independently and merged, so an unreachable or oddly formatted source is skipped without starving the others. When its insertion queue is empty, the pump refills it from the merged feeds, separates related-story HTML into individual headlines, and submits only exact matches from the bundled person-name index through the same judgment path. It uses no additional API or local inference model. Submissions occur at a random exponential interval with a default mean of 5 minutes per item; change that pacing with `--news-pump-interval` or `NEWS_PUMP_INTERVAL_SECONDS`.

The compact `names/person_index.json.gz` asset is generated from `names/person_2025_update.csv`. It deliberately excludes groups and single-token entries because bare surnames and words such as “Washington” are too ambiguous for a precision-first feed. Rebuild it after updating the source dataset with `python3 scripts/build_person_index.py`.

Successful results are stored at `data/history.jsonl`. Set `HISTORY_FILE` to an absolute path to use another location. To reset local history, stop the server and remove that file. Run one server process for this version; the file store is designed for one process.

The API key is read only on the server. Without it, the page still loads but submissions show a configuration error.

## Tests

```sh
python -m pytest -q
```

Tests use a fake TypeSafe response and do not spend API credits. A live smoke test requires a valid `TYPESAFE_API_KEY` and an actual submission through the page.

## Fly.io test deployment

The Fly App in `fly.toml` runs one Machine and one Gunicorn worker. Its JSONL history lives on the mounted `/data` volume. Do not scale beyond one Machine without moving history and Socket.IO coordination to shared services.

The TypeSafe key is a Fly runtime secret named `TYPESAFE_API_KEY`; never put its value in `fly.toml` or the Docker image. From this repository, use `fly deploy` to update the app. Check `fly status`, `fly checks list`, and `fly logs` after deployment. `fly machine list` and `fly volumes list` show the single Machine and its volume. For an emergency shutdown, `fly scale count 0` removes the Machine while retaining its volume; `fly deploy` recreates a Machine later. Stopping a Machine without scaling down is insufficient because incoming traffic can restart it.

The `.fly.dev` URL is unadvertised but publicly reachable. This initial deployment uses the TypeSafe account balance, with automatic refill disabled, as its external API spending limit. Fly compute and traffic charges are separate.

The product specification is in [docs/spec.md](docs/spec.md); the implementation plan is in [docs/build-plan.md](docs/build-plan.md).
