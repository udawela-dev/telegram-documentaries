# Requirements: The Bouncer — ADK vision gate (Phase 2)

## Context
Phase 2 of the ROADMAP. The Bouncer is the **first Google ADK agent** in the
pipeline: a Gemini 3.1 Flash Lite vision gate sitting in front of the Phase 1
gateway. It decides whether an uploaded photo contains a clearly discernible
human face or body, rejecting everything else with humour and resetting that
chat's ephemeral state.

## User's exact requirement
"When a user uploads a photo, download the image bytes and pass them to Gemini
3.1 Flash Lite for multimodal classification of whether a clearly discernible
human face or body is present. Human detected: approve the image and continue
the existing confirmation flow unchanged. No human detected: stop the pipeline,
reset ephemeral conversation state, and send a short, humorous rejection
explaining that the photo must contain a human. Reject objects, animals, food,
vehicles, landscapes, and other non-human images. If uncertain, reject safely.
Preserve the existing bot architecture and configuration. Do not hardcode API
keys or tokens. Add basic logging and error handling. Launch the bot and verify
a non-human photo is rejected and a clear person photo is approved."

## Scope

In scope:
- Photo message detection in the existing typed models (`Message.photo`).
- Telegram file download (getFile + file bytes; typed; no token leaks).
- One ADK agent (`bouncer`) on Gemini 3.1 Flash Lite, in-process session wiring
  (`InMemorySessionService` + `Runner`), structured decision + reason.
- The gate in the gateway dispatcher: text unchanged; photo → gate.
  Accepted → existing confirmation flow unchanged. Rejected → cheeky
  rejection + ephemeral per-chat state reset. Uncertain → reject safely.
- Download/classify failures: logged loudly, graceful message, loop survives.
- Ephemeral per-chat session store (ADK InMemorySessionService keyed by
  chat_id) with a `reset_chat(chat_id)` purge.
- `BOUNCER_MODEL` env override (default `gemini-3.1-flash-lite`); no hardcoded
  keys — reuse existing `load_settings` (TELEGRAM_BOT_TOKEN required,
  GEMINI_API_KEY required for gating).
- Tests: offline component/unit suite + guarded live positive/negative tests
  (fixtures committed under `tests/fixtures/` — see Out of scope note on
  .gitignore).
- Basic structured logging via existing `logging_utils` (log_call, event=,
  redaction filter including the Gemini key).

Out of scope:
- Interviewer, Converter, Scripter, Narrator (later phases).
- TTS, video, music, albums.
- Persistence/DB — state is ephemeral (dies with the process).
- Groups, multilingual, webhooks.
- Anything beyond the gate + reset for Phase 2 (Phase 3 owns the state machine).

## Technical contracts (from TECH.md)

- Typed Pydantic boundary for every external input/output (Telegram JSON,
  Gemini decision JSON). Never pass raw dicts across modules.
- Fail loudly and log for non-critical work; degrade gracefully on the user's
  conversation path; no bare `except: pass`, no swallowed exceptions.
- Never log/print/commit TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, image bytes.
- Red/Green TDD; `scripts/test` (pytest) + `scripts/hooks` are ground truth.
- README + `validation.md` updated to reflect what was actually implemented.

## Locked decisions

1. Model: `gemini-3.1-flash-lite` (env override `BOUNCER_MODEL`). If the exact
   string is rejected by the API at runtime, the implementer may use the
   closest available Flash Lite identifier and MUST record the exact string
   used in the code/README.
2. Decision contract: the agent must return STRICT JSON with at least
   `{"human_present": bool, "reason": str}`. Parsed into a Pydantic
   `BouncerDecision`; any parse failure or "uncertain" outcome counts as
   reject-safe (human_present=False).
3. Rejection copy (single constant `BOUNCER_REJECTION` in `src/bouncer.py`):
   `"Oi! 📸 No monsters, no sunsets, and definitely no last night's lasagna. I only do *humans* — a face, a torso, a faintly smug grin. Send me a picture of a person, mate."`
4. Download/classify failure message (single constant
   `BOUNCER_UNAVAILABLE_REPLY`): `"Hang on — I couldn't get a good look at that photo. Mind sending it again?"`
   A failure does NOT reset state and does NOT proceed to confirmation.
5. Accepted photo → the exact Phase 1 reply text (`"Hi Mate"`) continues,
   unchanged.
6. Photo choice: use the largest `PhotoSize` available in `message.photo`.
7. Session: one ADK session per chat_id; `reset_chat(chat_id)` deletes it.
   Chat isolation is a test requirement (one chat's reset never affects
   another).
8. Live tests skip unless `RUN_LIVE_GEMINI=1`; fixtures required (a human
   photo for positive, a non-human image for negative) under
   `tests/fixtures/`.

## Acceptance (from ROADMAP Phase 2)

- A portrait/person photo is approved and continues the confirmation flow.
- A non-portrait (object/landscape/animal/food/vehicle) photo is rejected in
  persona; the chat's ephemeral state resets.
- Either way the process stays alive and logs cleanly.
- Offline suite green; live tests produce positive (person) and negative
  (non-person) results.