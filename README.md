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

Open `http://127.0.0.1:5077` in one or more browser windows. Set `PORT` to use another port. Use `python app.py --host 0.0.0.0` to bind to all interfaces for access from other devices on your network. Submissions are rate limited per IP to 10 per minute and 500 per rolling 24 hours by default; change these with `--rate-limit-per-minute` and `--rate-limit-per-day`, or set either value to `0` to disable that specific limit. Every valid logical submission counts against these limits, including cache hits and attempts rejected because the evaluation queue is full; a transport replay using the same connection and request ID does not count twice.

The server runs up to four Jev evaluations concurrently and holds up to 32 additional unique evaluations in an in-process FIFO for at most 10 seconds. An IP can own at most four unique queued or running evaluations. Simultaneous requests with the exact same subject and Purgatory setting share one Jev call and all waiting clients receive its result. Exact repeats already in history use a read-only cache hit instead of calling TypeSafe or rewriting the history file. Add `--no-cache` to call TypeSafe for every accepted submission. The queue is intentionally process-local and is lost on restart; this deployment therefore remains limited to one Gunicorn worker and one Machine.

Use the numeric `127.0.0.1` address: on some Macs, `localhost:5000` reaches AirPlay instead of Flask and returns HTTP 403. If an older instance is already running on port 5000, use `http://127.0.0.1:5000` until you restart it. The application uses Socket.IO for all messages; HTTP serves only the page and static assets.

The news pump is disabled by default. Add `--news-pump` or set `NEWS_PUMP_ENABLED=1` to fetch names from news RSS in the background. It draws on a range of responsible mainstream sources across the US, UK, EU, and India (Google News regional editions plus outlets such as NPR, PBS, the BBC, The Guardian, The Independent, the Evening Standard, The Economist, France 24, Deutsche Welle, Fox News, and the Los Angeles Times); the feeds are fetched independently and merged, so an unreachable or oddly formatted source is skipped without starving the others. When its insertion queue is empty, the pump refills it from the merged feeds, separates related-story HTML into individual headlines, and submits only exact matches from the bundled person-name index through the same judgment path. It uses no additional API or local inference model. Submissions occur at a random exponential interval with a default mean of 5 minutes per item; change that pacing with `--news-pump-interval` or `NEWS_PUMP_INTERVAL_SECONDS`.

The compact `names/person_index.json.gz` asset is generated from `names/person_2025_update.csv`. It deliberately excludes groups and single-token entries because bare surnames and words such as “Washington” are too ambiguous for a precision-first feed. Rebuild it after updating the source dataset with `python3 scripts/build_person_index.py`.

Run the pump as a separate process with `python3 news-pump.py --url https://your-server.example --interval 300`. It uses the same feeds, deterministic person index, deduplication, and randomized exponential pacing as the built-in pump, but submits names through the server's `judgment:submit` Socket.IO event. Each submitted name is logged to the console by default; pass `--no-log` to suppress those hit messages. Pass `--log-feed-stats` to log the number of distinct matched names and previously unseen names from each feed whenever the queue is refilled. Names rejected because of rate limiting or server load are returned to the queue for a later attempt. In-flight names are also requeued if the Socket.IO connection drops.

Successful results are stored at `data/history.jsonl`. Set `HISTORY_FILE` to an absolute path to use another location. To reset local history, stop the server and remove that file. Run one server process for this version; the file store is designed for one process.

Fetch the saved feed history as JSON Lines with `GET /api/feed-history.jsonl`. The endpoint returns `application/x-ndjson` and an empty `200 OK` response if no history has been written yet.

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

## Citation

This project uses the [Pantheon dataset](https://pantheon.world/data/permissions): Pantheon by [Datawheel](https://datawheel.us) is licensed under a [Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/).
