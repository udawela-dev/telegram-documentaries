# TECH.md — Technical contract

Every later stage and agent defers to this file.

## Stack

| Concern | Choice |
| --- | --- |
| Language | Python |
| Bot transport | Telegram Bot API, **long polling** (`getUpdates`) |
| Agent framework | Google Agent Development Kit (ADK) |
| Vision gate / interview / script | Gemini 3.1 Flash Lite |
| Image generation | Gemini 3.1 Flash Image |
| Speech synthesis | `gemini-3.1-flash-tts-preview` |
| Secrets | `.env` → `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY` |

No webhook mode, no database, no cache layer, no extra services.

## Architecture

- **Hub-and-spoke.** The **Interviewer is the orchestrator**: it owns the
  conversation flow and decides which stage runs next.
- Each pipeline stage is a **discrete agent/module** with a single responsibility
  and an explicit input/output contract. Stages do not reach into each other's
  internals.
- The flow is an explicit **state machine** with named phases (e.g. awaiting
  photo → interviewing → converting → scripting → narrating → done). Illegal
  transitions are rejected, not guessed at.
- **The Narrator is not an agent.** The script is routed directly to Gemini TTS
  and delivered as audio — no LLM reasoning step.
- **The Converter returns the image directly to Telegram** with no intermediate
  text hop.

## Contracts at boundaries

- Parse Telegram updates, Gemini responses, and TTS output into **typed Pydantic
  models at the edge**.
- **Never pass raw dicts or unvalidated payloads across module boundaries.**
- Treat all external input (Telegram updates, file bytes, model output) as
  **untrusted and arbitrarily shaped** — validate before use, never assume
  keys, types, or lengths exist.

## Logging & error policy

- **Comprehensive structured logging.** Prefer **decorators** for entering,
  exiting, timing, and error reporting over sprinkling log calls through
  business logic.
- **On the user's conversation path:** catch errors and **degrade gracefully**
  so the conversation continues. Log loudly, never raise into the user's flow,
  never send a raw traceback to the chat.
- **For non-critical, user-invisible work:** **fail loudly and log.**
- Prohibited: bare `except: pass`, swallowed exceptions, un-logged fallbacks,
  silent `None` returns that hide a failure.

## Session state

- Held **in memory**, keyed by `chat_id`.
- **Versioned per-`chat_id` schema** so old sessions fail predictably rather
  than misbehave after a schema change.
- **One shared state driver** owns all reads/writes — stages never mutate state
  directly. The interview stage's driver now exists as `src/interview_state.py`:
  a versioned `InterviewState` (`schema_version`) keyed by `chat_id` (int) that
  spans the named phases `idle → interviewing → complete`, with the typed
  `UserProfile` as the hand-off contract to the next stage.
- **Reset semantics:** `/start` and `/restart` purge session state *and* any
  temporary media files, then return the flow to the initial phase. The process
  itself keeps running.
- Strict isolation: a `chat_id` can only ever read its own state.

## Testing

- **Red/Green TDD.** Tests are written *before* the code they specify.
- Happy path **and** edge cases (reset mid-flow, out-of-order input, non-human
  image, API timeout, malformed model response).
- Dev scripts live in `scripts/` — `scripts/test`, `scripts/hooks` — and are the
  **ground truth** for tests, lint, and type checks. They are documented here
  and in the README.

## Repo hygiene

- `.env` is in `.gitignore`; `.env.example` carries placeholders only.
- Reproducible environment: dependencies pinned via a lock/requirements file so
  a fresh clone installs identically.
- No secrets, no generated media, no cache artefacts in version control.

## README policy

`README.md` must document, and stay in sync with actual behaviour:

- setup and installation steps
- required environment variables
- how to run the bot
- what `scripts/test` and `scripts/hooks` do and when to run them
