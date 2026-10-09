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
- **The Narrator is not an agent.** The script is routed **directly** to Gemini
  TTS and delivered as audio — no LLM reasoning step, no ADK `LlmAgent`, no
  per-chat sessions. `src/narrator.py` calls
  `client.models.generate_content(model="gemini-3.1-flash-tts-preview", ...)`
  with `response_modalities=["AUDIO"]` and a single-speaker
  `SpeechConfig(voice_config=VoiceConfig(prebuilt_voice_config=PrebuiltVoiceConfig(voice_name=...)))`;
  a persona instruction (deep male, posh British, wildlife-documentary
  presenter) is embedded in the request text alongside `state.script`
  (NARRATOR_MODEL/NARRATOR_VOICE/NARRATOR_GEMINI_TIMEOUT overridable, bounded
  daemon-thread + Event timeout). Audio is staged through temp files with
  unpreventable `finally`-cleanup on success and error; a non-voice-note
  format is converted to OGG/Opus via an ffmpeg seam (`-f ogg` forced before
  the output path; missing ffmpeg when conversion is needed → loud
  `NarratorError`). **Deliberately no local/fake TTS fallback**: any failure
  degrades to the locked `NARRATOR_REPLY_UNAVAILABLE` — audio quality is never
  faked. Delivery via `send_voice`; a send failure logs
  `event=narrator_send_failed` loudly with no unavailable reply (the note may
  have been delivered). The Narrator adds no state fields — audio is
  transient, temp media purged.
- **The Bouncer's local fallback verdicts are labelled.** The vision gate's
  primary judge is the ADK `LlmAgent` on Gemini (Phase 2, `src/bouncer.py`);
  when Gemini is unreachable/missing a key the key-free **YuNet** face detector
  (`src/local_vision.py`) supplies the verdict and `BouncerDecision.source`
  becomes `"local"`. Local (YuNet) verdicts are labeled "(offline face check)"
  in the chat; Gemini verdicts keep the standard copy.
- **The Converter returns the image directly to Telegram** with no intermediate
  text hop: on completion the gateway sends the stored profile text, then makes
  **one multimodal ADK call** (`src/converter.py`) — the raw portrait
  (`inline_data` blob) plus the profile-grounded instruction in a single
  `Content`, `generate_content_config` requesting `response_modalities=["IMAGE"]`
  — and the generated image is sent straight back with `send_photo`.
- **The Converter's ADK sessions are strictly per-call.** Every `hybridize`
  starts from a **fresh** session (delete-then-create) and **reaps** it in a
  `finally` — a reused session would leak the previous portrait/prompt/image as
  inline history and keep those bytes in memory forever. `/start`, `/restart`
  and rejected photos also purge the chat's converter session via
  `Converter.reset_chat` (idempotent, chat-scoped; failures are logged, never
  fatal to the loop).
- **Key-free resilience:** on any Gemini failure/timeout/blocked key the
  Converter falls back to the deterministic OpenCV photo-booth composite
  (`src/local_composite.py`, archetypes cat/wolf/goat/owl/otter, decoded via the
  shared `decode_image_bytes` boundary in `src/local_vision.py`). With neither
  available it raises a loud `ConverterError` and the gateway replies
  `CONVERTER_REPLY_UNAVAILABLE` — never a silent fake image.
- **The Scripter produces exactly one 60–90 word paragraph** (`src/scripter.py`):
  an ADK `LlmAgent` on `gemini-3.1-flash-lite` receives one text turn embedding
  the completed `UserProfile` (summary + suggested animal) and returns a single
  British-wildlife-documentary paragraph, no markdown, TTS-ready. A
  deterministic local validator (`validate_script`: one paragraph block, word
  budget, markdown sniff) is the **single shape gate** for both generation
  paths — an off-spec output is never stored or handed to TTS — and triggers
  the key-free deterministic `LocalScriptWriter` fallback (`src/local_script.py`)
  whenever Gemini fails/times out/the key is blocked. Neither available →
  loud `ScripterError`, gateway replies locked `SCRIPTER_REPLY_UNAVAILABLE`,
  state untouched, loop survives.
- **Script delivery order:** after the profile text and the Converter's hybrid
  photo have been sent for a chat, the gateway runs the Scripter; the validated
  paragraph is the third message, and the Narrator's voice note the fourth and
  final (**profile text → hybrid photo → script text → voice note**). The
  Scripter persists the raw string on the shared driver (store first, then the
  gateway sends the same paragraph); a *delivery* failure logs
  `event=script_send_failed` loudly — the stored script stays for the Narrator
  and no misleading apology is sent. Sessions are per-call fresh and reaped
  (same rule as the Converter).

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
  a versioned `InterviewState` (`schema_version`, currently **2**) keyed by
  `chat_id` (int) that spans the named phases `idle → interviewing → complete`,
  with the typed `UserProfile` as the hand-off contract to the next stage.
  Phase 5 adds the nullable `script: str | None` field on the same driver (the
  raw TTS-ready paragraph for the Phase 6 Narrator); a v1 record is migrated
  losslessly to v2 (`script=None`, logged `event=state_migrated`) and a record
  newer than the current version fails loud.
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
