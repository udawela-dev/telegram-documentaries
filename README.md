# The Telegram Documentaries

Send a portrait to a Telegram bot and receive a narrated, comedy-wildlife
documentary about yourself.

**Current state: Phases 1 + 2 + 3 + 4.**

- **Phase 1 — the gateway.** A long-polling loop (`src/gateway.py`) that
  replies to text messages with `"Hi Mate"`.
- **Phase 2 — The Bouncer.** The project's **first Google ADK agent**
  (`src/bouncer.py`): an ADK `LlmAgent` on **Gemini 3.1 Flash Lite** that
  classifies an uploaded photo — does it contain a clearly discernible human
  face or body?
- **Phase 3 — The Interviewer.** The stateful orchestrator
  (`src/interviewer.py` + the shared per-chat state driver
  `src/interview_state.py`): after a human photo is approved it asks seven
  documentary-style questions, one at a time, and builds a behavioural profile
  plus a suggested animal. Question selection and the profile/animal builder are
  deterministic and local, so the interview works **key-free**; an ADK
  `LlmAgent` on **Gemini 3.1 Flash Lite** is wired as the backbone (decision
  2026-10-08).
- **Phase 4 — The Converter.** The portrait artist (`src/converter.py`): on
  the 7th answer the approved photo + the interview dossier go into **one
  multimodal call** on an ADK `LlmAgent` — **Gemini 3.1 Flash Image**
  (`response_modalities=["IMAGE"]`, no intermediate text hop) — and the hybrid
  animal portrait comes straight back to the chat via `send_photo`. Key-free
  resilience (`src/local_composite.py`): whenever Gemini is unreachable /
  timed out / the key is blocked, a deterministic OpenCV photo-booth composite
  is produced instead, so the pipeline always emits a real image today. The
  raw portrait is retained per chat (`src/portrait_store.py`) and purged on
  `/start`, `/restart`, and rejected photos (which also reset the Converter's
  ADK session).

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

Optional Interviewer tuning:

| Variable             | Default | What it does |
| -------------------- | ------- | ------------ |
| `INTERVIEWER_MODEL`  | `gemini-3.1-flash-lite` | Gemini model for the ADK interview backbone (question selection and the profile/animal builder are local and key-free). |

Optional Converter tuning:

| Variable                  | Default                | What it does |
| ------------------------- | ---------------------- | ------------ |
| `CONVERTER_MODEL`         | `gemini-3.1-flash-image` | Gemini image-generation model for the hybrid portrait. |
| `CONVERTER_GEMINI_TIMEOUT`| `60`                   | Seconds a Gemini image attempt may take before the local composite is used instead. |

Never commit `.env` — it is gitignored. Both secrets are scrubbed from every
log record by a redaction filter (`src/logging_utils.py`); the httpx/httpcore
loggers are silenced at the client so the token-bearing request URLs never leak.

## Run the bot

```bash
python main.py
```

Behaviour:

| You send | Bot replies |
| -------- | --------- |
| Text while idle (no interview running) | `Hi Mate` |
| Photo with a human | `Hi Mate` then **`Human detected ✓`**, then the interview's **Q1** (the interview starts) |
| Text while interviewing | Exactly **one** next question — the answer is stored in order first |
| 7th answer | Behavioural profile covering **Habits / Quirks / Routines / Preferences** plus **`Suggested animal: X`**, then a **hybrid portrait photo** of you as that animal arrives automatically |
| Text after the interview | Re-sends the stored profile + suggested animal |
| `/start` or `/restart` | Wipes the chat's Bouncer session **and** Interviewer state, purges the stored portrait + Converter session, then invites a fresh photo |
| Photo without a human (animal/object/landscape) | `Oi! 📸 No monsters, no sunsets… Send me a picture of a person, mate.` then **`Non-human detected`** (rejected + Bouncer session, Interviewer state, stored portrait and Converter session all reset) |
| Photo when Gemini is unreachable | **Local fallback verdict**: face detected → human verdict; no face → non-human verdict. (Only if the local detector fails too does the graceful "Hang on…" reply appear.) |

**The Interviewer state machine.** Interview progress lives on one shared
per-chat driver (`src/interview_state.py`), keyed by `chat_id`:
`idle → interviewing → complete`. Answers are stored in order before the next
question is asked, so an interrupted interview resumes at the right question
and two chats never see each other's state. Questions and the profile/animal
builder are a curated, deterministic local bank (`src/interviewer.py`), so the
interview works **key-free**; an ADK `LlmAgent` on `gemini-3.1-flash-lite` is
wired as the backbone per the 2026-10-08 decision.

**The Converter pipeline.** On completion the gateway sends the stored profile
text, then hands the saved portrait + profile to `Converter.hybridize`: one
ADK multimodal call (`portrait` bytes + instruction in a single `Content`,
`response_modalities=["IMAGE"]`), the generated image bytes are sent straight
back with `send_photo`. Every conversion starts from a **fresh, per-call ADK
session** (delete-then-create) and reaps it afterwards — no history or image
bytes linger in memory. If Gemini fails, times out (`CONVERTER_GEMINI_TIMEOUT`)
or the key is blocked, `LocalHybridComposer` produces a deterministic
photo-booth composite instead; with neither available the gateway replies
gracefully (`CONVERTER_REPLY_UNAVAILABLE`) and the loop survives.

Stop it with `Ctrl-C` (SIGINT) or SIGTERM — it shuts down gracefully.

## Checks (ground truth)

| Command       | What it does |
| ------------- | ------------ |
| `bash scripts/test`  | Runs the full pytest suite (unit, component, integration). |
| `bash scripts/hooks` | Pre-commit gate: syntax check (`compileall`) + `scripts/test`. |

Live Gemini verification is **opt-in** (offline suite never requires a key):

```bash
RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py tests/integration/test_live_converter.py -v
```

It classifies committed fixtures (`tests/fixtures/person.jpg` — expect
`human_present: true`; `tests/fixtures/non_human.jpg` — expect `false`) and
converts that portrait into a real `gemini-3.1-flash-image` hybrid, using the
key in `.env`.

## Architecture

`main.py` (entry point) → `src/config.py` (`.env` → validated `Settings`) →
`src/telegram_client.py` (raw-HTTP httpx client, incl. `getFile` + file
download for photos and `send_photo`) → `src/bouncer.py` (ADK agent + per-chat
in-memory sessions, with a hard 60s Gemini timeout) + `src/local_vision.py`
(key-free YuNet face detector, bundled model in `src/data/`, shared
`decode_image_bytes` boundary) → `src/gateway.py` (polling loop + dispatcher +
photo gate + interview routing + conversion) → `src/interviewer.py` (ADK
backbone + deterministic 7-question bank + animal matcher) backed by
`src/interview_state.py` (shared per-chat state driver) → `src/converter.py`
(ADK image agent on `gemini-3.1-flash-image`, one multimodal call, per-call
fresh + reaped sessions) with `src/portrait_store.py` (per-chat raw portrait)
and `src/local_composite.py` (key-free photo-booth fallback).

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