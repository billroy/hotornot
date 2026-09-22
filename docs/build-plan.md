# Heaven or Hell — staged implementation plan

Source of truth: [spec.md](spec.md). This plan implements the first version described there. Audience controls, moderation, and history retention stay deferred.

## Implementation choices

- **Server:** Python 3.10+ with Flask and Flask-SocketIO in threaded mode. Run one server process for this version so in-memory connection state and file writes have one owner. Serve only the HTML page and static assets over HTTP; all application messages use Socket.IO.
- **TypeSafe:** Call TypeSafe's documented System One HTTP API from the server. Keep `TYPESAFE_API_KEY` in the environment. Send the submitted string unchanged as `state`, `model="jev-latest"`, and one Choice with `instructions="Where should this one go?"` and `criteria={"heaven": null, "hell": null, "purgatory": null}`. Keep the API adapter isolated for testing. The HTTP API was used because the Python SDK package could not be fetched in this environment.
- **Client:** Use Vue.js for page state and the Socket.IO JavaScript client for events. Match client and server Socket.IO protocol versions. Render subject text through Vue text interpolation, never raw HTML.
- **History:** Store successful results in a server-side JSON Lines file under an application data directory excluded from source control. Load it once at startup. Serialize append, sequence assignment, and in-memory update under a lock; flush the appended record before broadcasting. File write failure means the submission fails and no result is broadcast. Use a monotonically increasing `sequence` on each result so clients can deduplicate and sort history across reconnects or concurrent completions. This extends the spec's proposed payload without changing its behavior.
- **Input:** Reject malformed payloads and strings that are empty after whitespace checking. Choose a modest explicit maximum length during implementation. Preserve every character of accepted input in the TypeSafe `state`, result, and history file.
- **Concurrency:** Run the blocking TypeSafe call outside the Socket.IO event handler's request path in a background task. Keep the page responsive while other evaluations run. Bound concurrent evaluations to prevent accidental API bursts; report a retryable error to the submitting browser when capacity is full.

## Stage 1 — Project skeleton and configuration

Create a small Flask application, dependency manifest, page template, static files, environment configuration, and a short README with local run instructions. Add an ignored data directory for history and document `TYPESAFE_API_KEY`. Verify the page loads and a browser can establish a Socket.IO connection. Do not expose an application REST endpoint.

**Gate:** Start the server locally; load the page; confirm a Socket.IO connect succeeds and no API key appears in served files.

## Stage 2 — TypeSafe evaluation boundary

Implement one function that accepts the original subject string and returns a validated result containing the chosen destination, all three numeric probabilities, and confidence. Check that the response has exactly the three expected option keys, finite values in `[0, 1]`, and a valid chosen key; tolerate small floating-point sum error. Convert provider failures into safe application errors, with useful server logs that omit the API key and avoid echoing raw user text unnecessarily. Configure a finite timeout and bounded backoff for rate limits and overload.

**Gate:** With a fake TypeSafe client, verify the exact request shape and unchanged state; verify malformed or failed provider responses cannot become successful results. Make one optional live smoke call only when a real API key is available.

## Stage 3 — File-backed history and Socket.IO flow

Implement the four events from the spec: `judgment:submit`, `judgment:result`, `judgment:error`, and `judgment:history`. Assign a server result ID, UTC timestamp, and sequence after evaluation succeeds. Persist the result before broadcasting it to all clients. On connection, send the file-backed history to that browser. Route errors only to the submitting socket, correlated by `request_id`. Keep one in-process history list ordered by sequence and protect persistence plus sequence assignment with a lock. Reject invalid input before calling TypeSafe.

**Gate:** Socket.IO test clients show that one submission reaches two connected clients once; a late client receives history; a fresh server instance reloads the same history file; invalid input and provider/file errors add no history entry. Exercise concurrent submissions and verify unique IDs, sequence order, and intact JSON Lines records.

## Stage 4 — Vue page and interaction

Build the single page with a subject field, submit action, pending and connection states, error feedback, and the history beneath the form. Show Heaven, Hell, and Purgatory probabilities as labeled percentages, the winning choice, and the separate confidence value. Match completion or error to the browser-generated `request_id`. Disable duplicate submission while that request is pending. Merge initial history and live results by server ID, then order by descending sequence so the newest result stays at the top and reconnects do not duplicate entries. Keep the entertainment framing visible.

**Gate:** In two browser windows, submit from either window and confirm both update without reload. Confirm repeat submissions build history, errors are visible only to the submitter, disconnect/reconnect restores history, and subject text displays literally even when it contains HTML-like characters.

## Stage 5 — End-to-end verification and handoff

Run the focused tests and manually check the complete flow against every acceptance criterion in the spec. Restart the server to verify persisted history. Check that browser traffic carries application data through Socket.IO and that the TypeSafe key stays server-side. Document setup, required environment variables, local start command, history file location, and how to reset local history. Record any live API verification that could not be run without credentials.

**Exit condition:** A fresh checkout can be configured and run from the README, and the verified app meets the spec's acceptance criteria.

## Known limits for this version

The initial deployment uses one server process and an ever-growing history file, with the full history sent to each new connection. Multi-process deployment, pagination, retention, moderation, and audience controls require a later design pass.
