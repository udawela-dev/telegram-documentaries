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
| `GEMINI_API_KEY`     | no*      | yes — the Bouncer's vision brain |

\* Without `GEMINI_API_KEY` the bot still runs its text flow, and photo
uploads are still classified — by the **key-free local fallback**. If Google
reports your key as *leaked* (403 `PERMISSION_DENIED`), it is permanently
disabled server-side — create a fresh key **in a different project/account**
and put it in `.env`.

Optional Bouncer tuning:

| Variable                    | Default | What it does |
| --------------------------- | ------- | ------------ |
| `BOUNCER_MODEL`             | `gemini-3.1-flash-lite` | Gemini vision model. |
| `BOUNCER_GEMINI_TIMEOUT`    | `60`    | Seconds a Gemini verdict may take before falling back to the local detector. |
| `BOUNCER_FACE_MODEL`        | bundled | Path to the YuNet ONNX model (overrides `src/data/`). |
| `BOUNCER_YUNET_THRESHOLD`   | `0.3`   | Face-detection confidence floor (0-1). |

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
| Photo when Gemini is unreachable | **Local fallback verdict**: face detected → `Hi Mate`; no face → rejection. (Only if the local detector fails too does the graceful "Hang on…" reply appear.) |

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
download for photos) → `src/bouncer.py` (ADK agent + per-chat in-memory
sessions, with a hard 60s Gemini timeout) + `src/local_vision.py` (key-free
YuNet face detector, bundled model in `src/data/`) → `src/gateway.py`
(polling loop + dispatcher + photo gate).

The Bouncer's verdict order: **Gemini (ADK agent) first**; on failure or
timeout it falls back to the local YuNet detector so a photo always gets a
real human/non-human answer; only if neither can judge does the gateway reply
gracefully. Chat state resets stay Gemini-session-based and chat-scoped.

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