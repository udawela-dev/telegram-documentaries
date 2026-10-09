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
  `NarratorError`). The seam's input format is decided from the response
  **mime**, not the bytes (2026-10-09, live-verified): the TTS model returns
  headerless LINEAR16 PCM (`audio/L16;codec=pcm;rate=24000`, mono s16le), so a
  declared LINEAR16 mime stages `.pcm` + explicit `-f s16le -ar <rate> -ac 1`;
  RIFF/WAVE stages `.wav` and is probed; anything else stages an opaque `.tmp`
  probed by content so an unknown format fails loudly instead of being
  force-decoded as PCM. **Deliberately no local/fake TTS fallback**: any failure
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

## Presenter personas (Phase 8)

- **Two presenters, selectable per chat:** `attenborough` (posh British, the
  default) and `irwin` ("Crikey!" energy). `src/persona.py` is the **single
  owner of persona copy** (stdlib-only — imported by `src/narrator.py` without
  a circular import): the typed `Persona` enum, script-tone snippets, per-persona
  prebuilt TTS voices and narrator instructions, and the local-writer openers.
  Everything reads from it; nothing hardcodes persona strings elsewhere.
- **Selection and dispatch:** `/persona <token>` sets the chat's choice
  (in-memory store inside the Gateway); `/persona` alone → locked usage;
  unknown token → locked usage + `event=persona_unknown_reply`. The choice is
  resolved **at dispatch time** (`_resolve_persona`): stored choice wins,
  else `PERSONA_DEFAULT` env, else `attenborough` (invalid env → loud
  `event=persona_default_invalid` + fallback, never a crash). The store is
  **never cleared by resets** — the persona survives `/restart`/`/start`.
- **The persona colours exactly two stages** (Bouncer/Interviewer copy stays
  locked, user decision 2):
  - **Scripter** — `write_script(chat_id, profile, persona)`; the tone snippet
    is prepended to the model instruction (`persona_instruction`), and
    `LocalScriptWriter` gets the opener. Attenborough's opener is empty →
    a persona-less Phase 7 run is **byte-for-byte identical** (env absent =
    no env, unit-locked).
  - **Narrator** — `synthesize(chat_id, script, persona)`; the prebuilt voice
    (`resolve_persona_voice`: `NARRATOR_VOICE` → default `Orus` for
    attenborough, `NARRATOR_VOICE_IRWIN` → default `Charon` for irwin) and the
    style instruction are applied **per call**: set before the TTS worker
    starts, cleared in the same `finally` that reaps temp files, so the worker
    only ever sees this call's voice. Free-text personas are a typed `TypeError`
    boundary at both stages.
- **Unknown-command hardening (user decision 4):** any `/word` outside
  `_KNOWN_COMMANDS = {"/start", "/restart", "/persona"}` gets the locked
  `GATEWAY_REPLY_UNKNOWN_COMMAND` reply **at every phase** — handled before
  the interview-phase read, so it is never stored as an interview answer and
  the interview position never moves (unit- and component-locked). Matching is
  token-based (`_command_name`), so `/startle` is unknown, not a reset.

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
- **Reset semantics (Phase 7):** `/start` and `/restart` are **identical** —
  an instant per-chat reset from **any** phase (idle/interviewing/complete),
  with **no confirmation prompt**. The sweep purges the Bouncer, Interviewer,
  Converter **and Scripter** sessions, the stored portrait, and **all temp
  assets registered for that chat** (`src/temp_assets.py` — the Narrator
  tracks every staged audio file at creation) for the same `chat_id`; each
  per-stage failure is logged loud and never fatal to the loop, and the
  process keeps running. **Stale-task invalidation is by construction, not by
  token:** dispatch is strictly sequential (single-threaded `poll_once`; every
  stage call blocks up to its timeout), so a reset can never interleave
  mid-generation — it is honoured at the next free step, and an in-flight
  step's deliveries complete before the purge applies. Stage timeout workers
  are daemon threads that can neither send to Telegram nor write shared state
  (`test_reset_queued_behind_a_stage_update_is_honoured_at_the_next_free_step`
  locks the guarantee; the originally planned epoch guard is unreachable code
  in this architecture and was retired by user decision). A reset session may
  begin again immediately (the next photo re-enters at `IDLE`).
- **Wrong payload at the wrong stage (Phase 7):** the interview phase is
  checked **before** the gate — a photo sent while `INTERVIEWING` or
  `COMPLETE` is refused with the locked `GATEWAY_REPLY_PHOTO_DURING_INTERVIEW`
  and never reaches the Bouncer, portrait store, or interview state (the gate
  runs **only** from `IDLE`). Text at `IDLE` now prompts for a portrait
  (`GATEWAY_REPLY_NEED_PHOTO`, logged `event=idle_text_photo_prompt`) instead
  of the bare "Hi Mate" confirmation; non-photo media (video/document/sticker)
  is routed by phase through `_handle_media` (`event=media_no_photo`) with the
  same copy; duplicate `update_id`s in one batch are applied once
  (`event=skip_duplicate_update`). Every degraded stage reply (Bouncer /
  Converter / Scripter / Narrator unavailable, photo download failure) is
  followed by one locked retry hint `GATEWAY_REPLY_RETRY_HINT`.
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
