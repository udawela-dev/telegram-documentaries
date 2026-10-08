# The Telegram Documentaries

Send a portrait to a Telegram bot and receive a narrated, comedy-wildlife
documentary about yourself.

**Current state: Phases 1 + 2.**

- **Phase 1 — the gateway.** A long-polling loop (`src/gateway.py`) that
  replies to text messages with `"Hi Mate"`.
- **Phase 2 — The Bouncer.** The project's **first Google ADK agent**
  (`src/bouncer.py`): an ADK `LlmAgent` on **Gemini 3.1 Flash Lite** that
  classifies an uploaded photo — does it contain a clearly discernible human
  face or body?

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
```

## Environment variables

Copy `.env.example` to `.env` and fill in real values:

| Variable             | Required | Used in Phase 2? |
| -------------------- | -------- | ---------------- |
| `TELEGRAM_BOT_TOKEN` | yes      | yes — long polling auth |
| `GEMINI_API_KEY`     | yes*     | yes — the Bouncer's vision brain |

\* Without `GEMINI_API_KEY` the bot still runs its text flow; photo uploads get
a graceful "couldn't get a good look" reply instead of a verdict. If Google
reports your key as *leaked* (403 `PERMISSION_DENIED`), it is permanently
disabled server-side — create a fresh key **in a different project/account**
and put it in `.env`.

Optional: `BOUNCER_MODEL` overrides the vision model (default
`gemini-3.1-flash-lite`).

Never commit `.env` — it is gitignored. Both secrets are scrubbed from every
log record by a redaction filter (`src/logging_utils.py`); the httpx/httpcore
loggers are silenced at the client so the token-bearing request URLs never leak.

## Run the bot

```bash
python main.py
```

Behaviour:

| You send | Bot reply |
| -------- | --------- |
| Text | `Hi Mate` |
| Photo with a human | `Hi Mate` (approved — confirmation flow continues) |
| Photo without a human (animal/object/landscape) | `Oi! 📸 No monsters, no sunsets… Send me a picture of a person, mate.` (rejected + chat state reset) |
| Photo when Gemini is unreachable | `Hang on — I couldn't get a good look at that photo…` (graceful; loop survives) |

Stop it with `Ctrl-C` (SIGINT) or SIGTERM — it shuts down gracefully.

## Checks (ground truth)

| Command       | What it does |
| ------------- | ------------ |
| `bash scripts/test`  | Runs the full pytest suite (unit, component, integration). |
| `bash scripts/hooks` | Pre-commit gate: syntax check (`compileall`) + `scripts/test`. |

Live Gemini verification is **opt-in** (offline suite never requires a key):

```bash
RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py -v
```

It classifies committed fixtures (`tests/fixtures/person.jpg` — expect
`human_present: true`; `tests/fixtures/non_human.jpg` — expect `false`) using
the key in `.env`.

## Architecture

`main.py` (entry point) → `src/config.py` (`.env` → validated `Settings`) →
`src/telegram_client.py` (raw-HTTP httpx client, incl. `getFile` + file
download for photos) and `src/bouncer.py` (ADK agent + per-chat in-memory
sessions) → `src/gateway.py` (polling loop + dispatcher + photo gate).

Key rules from `SPECS/` honoured here:

- **Typed boundaries** — raw Telegram JSON is parsed into Pydantic models
  (`src/telegram_models.py`) before it is used; the agent's strict-JSON verdict
  is parsed into a `BouncerDecision`; malformed input is never fatal.
- **Long polling** — updates consumed with an advancing `offset`, so the same
  update is never processed twice.
- **Reject safe** — if the Bouncer is uncertain or the output doesn't parse,
  the photo is rejected (never accidentally approved).
- **Resilience** — failed polls, failed downloads, failed classifications and
  failed state resets are logged; the loop keeps running; one bad update never
  kills the bot. Session resets are chat-scoped (one chat's rejection never
  clears another chat's state).
- **Logging policy** — structured decorator-based logging
  (`src/logging_utils.py`); no bare `except: pass`, no swallowed errors, no
  un-logged fallbacks.