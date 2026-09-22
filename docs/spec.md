# Heaven or Hell — product and interface specification

## Goal

Build a shared, playful web page where a visitor enters a name or concept and sees Jev's probability distribution across **Heaven**, **Hell**, and **Purgatory**. Every connected browser receives each completed result. The page keeps a growing list of completed results beneath the input so people can play repeatedly and watch the shared history grow.

The result is a model judgment made for entertainment. The interface must not present it as a factual claim about a person or a real afterlife.

## User experience

1. A visitor opens the main page and sees one text field, a submit action, and the shared result history below.
2. The visitor enters a nonempty name or concept and submits it. The page shows that the submission is pending and prevents accidental duplicate submission. The submitted text is passed to Jev unchanged.
3. The Flask server receives the text through a Socket.IO event, asks TypeSafe Jev one Choice question, and receives a selected option and probabilities for all three options.
4. The server saves and broadcasts the completed result to all connected Socket.IO browsers. Each browser puts it at the top of the shared history and displays the three probabilities as percentages, the selected option, and Jev's confidence value.
5. Any visitor can submit another entry without reloading the page. A failed request shows an error to its submitter and does not add a fabricated result to history.

The UI uses Vue.js for reactive input, pending state, error state, and history rendering. Flask serves the page and runs the Socket.IO server. Browser-to-server application data and server-to-browser results use Socket.IO exclusively; there are no application REST endpoints. Normal HTTP requests to load the page and static assets are permitted.

## Jev evaluation

Use TypeSafe's System One API with `model: "jev-latest"`, the exact text the visitor submitted as `state`, and one `choice` question with three criteria keys: `heaven`, `hell`, and `purgatory`. Set the Choice `instructions` field to exactly `Where should this one go?`. Do not add context, a rubric, disambiguation, or other text to the submitted state. Let Jev infer the subject from that text. Use `null` descriptions for the three criteria so no extra interpretation is supplied through the options.

The TypeSafe response is expected to contain `answers.<question_id>.choice`, `confidence`, and `probabilities` for all three options. The server validates this response before saving or broadcasting. It sends the original numeric probabilities, selected option, and confidence; the browser formats the probabilities as percentages and displays confidence separately. Small display rounding differences are acceptable. `confidence` is distinct from the winning option's probability.

TypeSafe documentation: [Introduction](https://docs.typesafe.ai/introduction), [Choice](https://docs.typesafe.ai/primitives/choice), [Quick start](https://docs.typesafe.ai/introduction/quickstart), [API reference](https://docs.typesafe.ai/api).

## Socket.IO message contract

Event names and payloads below are the proposed application contract. The implementation may refine field names during planning, but must preserve the behavior.

| Direction | Event | Payload | Purpose |
| --- | --- | --- | --- |
| Browser → server | `judgment:submit` | `{ request_id: string, subject: string }` | Submit one name or concept. The browser creates a unique request ID to match errors or completion to its pending input. |
| Server → all browsers | `judgment:result` | `{ id: string, request_id: string, subject: string, choice: "heaven" \| "hell" \| "purgatory", probabilities: { heaven: number, hell: number, purgatory: number }, confidence: number, created_at: string }` | Announce one completed evaluation. `id` is unique for history deduplication; `created_at` is a server timestamp. |
| Server → submitting browser | `judgment:error` | `{ request_id: string, message: string }` | Report validation or TypeSafe failure without broadcasting a result. The message is safe for display and contains no API key or raw provider error body. |
| Server → connecting browser | `judgment:history` | `{ results: JudgmentResult[] }` | Initialize the shared history from results loaded from the history file and added since startup. |

The server is authoritative for result IDs, timestamps, and history order. Successful results are saved to a file and broadcast once. Browsers display the newest result first, including when loading saved history. The server reloads the file on startup. A newly connected browser receives the complete saved history; subsequent result events continue that list. The server rejects text that is empty or whitespace only and applies a length limit, but passes valid submitted text to Jev unchanged. It rejects malformed submissions without calling TypeSafe.

## Operational behavior

- The TypeSafe API key stays on the server, supplied by environment configuration; it is never sent to browsers.
- Multiple visitors may submit concurrently. Each request completes independently. History follows server completion order, so a slower earlier request may appear later.
- Completed results persist in a server-side file and are reloaded at startup. Failed evaluations do not enter the file. The file location, format, and concurrent-write strategy are implementation details.
- During an upstream error, timeout, or invalid response, the submitter sees a recoverable error and can retry. The other browsers receive no result for that failed request.
- User supplied subject text must render as text, not HTML.

## Acceptance criteria

- A visitor can enter a name or concept and submit it without a page reload or application REST call.
- The backend makes one TypeSafe Choice evaluation per accepted submission using exactly the three destination options.
- A successful result displays all three probabilities, the selected destination, and confidence on every browser connected at broadcast time.
- Repeated submissions accumulate as separate entries in a history list below the input, with the newest entry at the top.
- A browser that connects later sees the saved history, including results from before the latest server restart.
- Invalid input and provider failures produce visible errors for the submitter without adding a false history entry.
- The TypeSafe credential is unavailable in browser assets and Socket.IO payloads.

## Deferred to a later phase

Audience scope, content rules, and moderation are outside this first version. All connected browsers receive submitted subjects and results under the shared-history behavior above.

History retention is also deferred: the first version keeps every successful result in the file and sends the full list to newly connected browsers. That list will grow indefinitely unless a later phase adds a retention or pagination policy.
