# The Telegram Documentaries

Send a portrait to a Telegram bot and receive a narrated, comedy-wildlife
documentary about yourself.

**Current state: Phases 1–8.** (gateway → Bouncer → Interviewer → Converter →
Scripter → Narrator → resilience & polish → presenter personas)

- **Phase 1 — the gateway.** A long-polling loop (`src/gateway.py`) that
  replies to text messages with `"Hi Mate"`.
- **Phase 2 — The Bouncer.** The project's **first Google ADK agent**
  (`src/bouncer.py`): an ADK `LlmAgent` on **Gemini 3.1 Flash Lite** that
  classifies an uploaded photo — does it contain a clearly discernible human
  face or body? When Gemini is unreachable it falls back to the key-free local
  **YuNet** face detector; those offline verdicts are labelled
  **`(offline face check)`** in the chat, because the local detector only finds
  faces (it cannot tell an animal face from a human one).
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
- **Phase 5 — The Scripter.** The narrator (`src/scripter.py`): once the
  profile text *and* the hybrid photo have been delivered, an ADK `LlmAgent`
  on **Gemini 3.1 Flash Lite** turns the behavioural dossier into **exactly
  one 60–90 word British-wildlife-documentary paragraph** (no markdown, TTS
  ready) which arrives in the chat as the final message of the run — order:
  **profile text → hybrid photo → script text**. The raw script string is
  stored on the shared per-chat state for the Phase 6 voice note. Key-free
  resilience (`src/local_script.py`): a deterministic local writer produces
  the paragraph whenever Gemini fails, times out, the key is blocked, or the
  output breaks the one-paragraph/word-budget contract; a local validator
  (`validate_script`) is the single shape gate for both paths, so an
  off-spec "script" is never stored or spoken.
- **Phase 6 — The Narrator.** The voice (`src/narrator.py`): **directly** — not
  as an ADK agent — calls **Gemini 3.1 Flash TTS** (`gemini-3.1-flash-tts-preview`,
  `response_modalities=["AUDIO"]` + a single-speaker prebuilt voice) with the
  stored script as input, and delivers the resulting narration as a Telegram
  **voice note** via `send_voice` — the fourth and final message of a run.
  Voice direction is a deep male, posh-British wildlife-documentary presenter
  (prebuilt voice + persona instruction; `NARRATOR_VOICE` overridable). Audio
  is staged through temp files with cleanup in a `finally` on success and
  error; if the API returns a non-voice-note format it is converted to
  OGG/Opus via ffmpeg (missing ffmpeg → graceful unavailable, never silent).
  **No local/fake TTS fallback** (deliberate): when TTS cannot run the bot
  sends the locked `NARRATOR_REPLY_UNAVAILABLE` — audio quality is never
  faked.
- **Phase 7 — Resilience & polish.** (`feature/2026-10-09-reset-hardening`):
  `/start` and `/restart` are identical per-chat resets — instant, no
  confirmation, from **any** phase — that purge the Bouncer/Interviewer/
  Converter/**Scripter** sessions, the stored portrait, and **every temp asset
  registered for that chat** (`src/temp_assets.py`). Stale results are
  impossible **by construction**: dispatch is strictly sequential, so a reset
  always lands at the next free step and a busy stage's workers can never
  send late output. Wrong payloads are refused at the right
  stage: a photo during the interview or after completion never re-runs the
  gate (`please answer the current question with text, or type /restart`),
  text while idle now prompts for a portrait instead of a bare "Hi Mate",
  non-photo media is routed by phase, duplicate updates in one batch apply
  once, and every degraded Gemini reply is followed by a retry hint
  (`try again, or type /restart`).
- **Phase 8 — Presenter personas.** (`feature/2026-10-09-presenter-personas`):
  two presenters, selectable **per chat** with `/persona attenborough` (posh
  British, the default) or `/persona irwin` ("Crikey!" energy). The choice is
  in-memory, survives `/restart`, and colours exactly two stages — the
  **Scripter** (tone: attenborough = today's deep posh-British paragraph,
  irwin = "Crikey!"-flavoured opener) and the **Narrator** (distinct prebuilt
  TTS voice per persona: `NARRATOR_VOICE` for attenborough, `NARRATOR_VOICE_IRWIN`
  for irwin, default `Charon`; both driven by real Gemini TTS). Bouncer and
  Interviewer copy stays locked; all persona copy lives in `src/persona.py`
  (the single owner, wired into gateway/scripter/narrator). An unknown
  `/command` at any phase gets the locked unknown-command reply and is never
  stored as an interview answer nor does it advance the interview.

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

Optional Scripter tuning:

| Variable                | Default                | What it does |
| ----------------------- | ---------------------- | ------------ |
| `SCRIPTER_MODEL`        | `gemini-3.1-flash-lite` | Gemini text model for the narration paragraph. |
| `SCRIPTER_GEMINI_TIMEOUT` | `60`                 | Seconds a Gemini script attempt may take before the key-free local writer is used instead. |

Optional Narrator tuning:

| Variable                 | Default                     | What it does |
| ------------------------ | --------------------------- | ------------ |
| `NARRATOR_MODEL`         | `gemini-3.1-flash-tts-preview` | Gemini TTS model for the voice note (direct API call, not an agent). |
| `NARRATOR_VOICE`         | `Orus`                  | Prebuilt Gemini TTS voice (firm/low-register male). |
| `NARRATOR_VOICE_IRWIN`   | `Charon`                | Prebuilt Gemini TTS voice for the `irwin` persona. |
| `NARRATOR_GEMINI_TIMEOUT`| `60`                        | Seconds a TTS synthesis may take before it is abandoned (graceful unavailable — there is no local TTS fallback). |

Optional presenter tuning (Phase 8):

| Variable          | Default        | What it does |
| ----------------- | -------------- | ------------ |
| `PERSONA_DEFAULT` | `attenborough` | The presenter for chats that have never run `/persona` (invalid/blank value logs `event=persona_default_invalid` and falls back to `attenborough`). |

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
| Text while idle (no interview running) | `upload a clear portrait photo` — the loop asks for the portrait (Phase 7; no bare "Hi Mate") |
| Photo with a human | `Hi Mate` then **`Human detected ✓`**, then the interview's **Q1** (the interview starts) |
| Text while interviewing | Exactly **one** next question — the answer is stored in order first |
| 7th answer | Behavioural profile covering **Habits / Quirks / Routines / Preferences** plus **`Suggested animal: X`**, then a **hybrid portrait photo** of you as that animal, then the **scripted narration** — one dramatic 60–90 word paragraph about you — and finally a **voice note** of the narrator reading that paragraph (order: profile text → hybrid photo → script text → voice note) |
| Text after the interview | Re-sends the stored profile + suggested animal |
| Photo while an interview is running (or after completion) | `please answer the current question with text, or type /restart` — the gate is **never** re-run and no state is touched |
| `/start` or `/restart` | Wipes the chat's Bouncer, Interviewer, Converter **and Scripter** sessions, purges the stored portrait + all registered temp files — instant reset from any phase, no confirmation, then invites a fresh photo (dispatch is strictly sequential, so the reset always lands cleanly at the next free step) |
| `/persona attenborough` or `/persona irwin` | Picks that chat's presenter (per-chat, in-memory, survives `/restart`): `attenborough` = today's posh-British narrator; `irwin` = "Crikey!"-flavoured script **and** a distinct real TTS voice (`Charon`). Confirmations and usage copy are locked (`/persona` alone prints the usage) |
| Any other `/command` (e.g. `/pizza`) | `unknown command` — the locked reply at **every** phase; it is never stored as an interview answer and the interview position never moves |
| Photo without a human (animal/object/landscape) | `Oi! 📸 No monsters, no sunsets… Send me a picture of a person, mate.` then **`Non-human detected`** (rejected + Bouncer session, Interviewer state, stored portrait and Converter session all reset; a rejection only happens while idle) |
| Photo when Gemini is unreachable | **Local fallback verdict**, labelled as offline: face detected → **`Human detected ✓ (offline face check)`**; no face → **`Non-human detected (offline face check)`**. The local detector only finds faces, so an animal can pass this weak gate (the real discriminator is Gemini). (Only if the local detector fails too does the graceful "Hang on…" reply appear.) |

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

**The Scripter stage.** After the hybrid photo, the gateway runs the Scripter
for that chat: `write_script` asks Gemini (one text turn embedding the profile
summary + suggested animal) for exactly one 60–90 word documentary paragraph,
validates the shape (`validate_script` — one paragraph, word budget, no
markdown), persists the raw string on the shared per-chat state (`script`
field, schema `v2`), and the gateway sends the same paragraph back as the
final text message. Any Gemini failure/timeout/blocked-key/off-shape output
falls back to the deterministic `LocalScriptWriter`; if nothing can produce a
valid paragraph the chat gets `SCRIPTER_REPLY_UNAVAILABLE` and nothing is
stored. Like the Converter, each call starts from a fresh ADK session and
reaps it afterwards.

**The Narrator stage.** After the script text is sent, the gateway runs the
Narrator for that chat: `synthesize` reads the stored `script` from the shared
state and makes a **direct** `gemini-3.1-flash-tts-preview` call
(`response_modalities=["AUDIO"]`, single-speaker prebuilt voice, persona
instruction for a deep posh-British wildlife presenter) — explicitly no ADK
agent, no reasoning step. The audio is staged through temp files (converted to
OGG/Opus via ffmpeg when the API returns another format, with the output
format forced), sent with `send_voice`, and the temp files are unlinked in a
`finally` on success and error. A missing script, API error, timeout, or
missing-ffmpeg-when-needed logs loudly and the chat receives the locked
`NARRATOR_REPLY_UNAVAILABLE` ("Hang on — the narrator lost his voice. Give
that another go?"); a *send* failure logs `event=narrator_send_failed` but no
unavailable reply (the script and voice may have reached the chat). There is
**no local/fake TTS fallback** — audio quality is never faked.

Stop it with `Ctrl-C` (SIGINT) or SIGTERM — it shuts down gracefully.

## Checks (ground truth)

| Command       | What it does |
| ------------- | ------------ |
| `bash scripts/test`  | Runs the full pytest suite (unit, component, integration). |
| `bash scripts/hooks` | Pre-commit gate: syntax check (`compileall`) + `scripts/test`. |

Live Gemini verification is **opt-in** (offline suite never requires a key):

```bash
RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py tests/integration/test_live_converter.py tests/integration/test_live_scripter.py tests/integration/test_live_narrator.py tests/integration/test_live_personas.py -v
```

It classifies committed fixtures (`tests/fixtures/person.jpg` — expect
`human_present: true`; `tests/fixtures/non_human.jpg` — expect `false`),
converts that portrait into a real `gemini-3.1-flash-image` hybrid, writes
a real `gemini-3.1-flash-lite` 60–90 word script from a seeded profile, and
synthesizes a real `gemini-3.1-flash-tts-preview` voice note from a seeded
script, using the key in `.env`.

## Architecture

`main.py` (entry point) → `src/config.py` (`.env` → validated `Settings`) →
`src/telegram_client.py` (raw-HTTP httpx client, incl. `getFile` + file
download for photos and `send_photo`) → `src/bouncer.py` (ADK agent + per-chat
in-memory sessions, with a hard 60s Gemini timeout) + `src/local_vision.py`
(key-free YuNet face detector, bundled model in `src/data/`, shared
`decode_image_bytes` boundary) → `src/gateway.py` (polling loop + dispatcher + photo gate + interview routing +
conversion + scripting) → `src/interviewer.py` (ADK
backbone + deterministic 7-question bank + animal matcher) backed by
`src/interview_state.py` (shared per-chat state driver) → `src/converter.py`
(ADK image agent on `gemini-3.1-flash-image`, one multimodal call, per-call
fresh + reaped sessions) with `src/portrait_store.py` (per-chat raw portrait)
and `src/local_composite.py` (key-free photo-booth fallback) →
`src/scripter.py` (ADK text agent on `gemini-3.1-flash-lite`, one 60–90 word
documentary paragraph, `validate_script` shape gate, per-call fresh + reaped
sessions) with `src/local_script.py` (key-free deterministic writer) →
`src/persona.py` (Phase 8 single owner of presenter copy — typed `Persona`,
script tones, per-persona TTS voices/instructions — wired into the gateway's
`/persona` command, the Scripter and the Narrator) → `src/narrator.py`
(**direct** `gemini-3.1-flash-tts-preview` call — not an
agent — voice note via `send_voice`; temp-file lifecycle with
`finally`-cleanup; ffmpeg OGG/Opus conversion seam when needed; no local TTS
fallback).

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