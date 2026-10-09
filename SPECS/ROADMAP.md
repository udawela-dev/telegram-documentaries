# ROADMAP.md — Build order

One phase per pipeline capability. A phase is **complete only when verified**
against its acceptance criteria — mark it done on proof, not on intent.

---

### 1. Repository & gateway

Project skeleton, `.env` loading, and a long-polling loop that dispatches
updates and replies with a hardcoded message.

**Acceptance:** `scripts/test` passes; the bot receives an update over long
polling and replies. No webhook, no public URL.
**Rubric:** transport works end to end.

---

### 2. Bouncer — vision gate

Gemini 3.1 Flash Lite vision classification: confirm a human is present.
Cheekily reject non-human images and route to a state reset. Text/image
classification only — **no TTS, no video**.

**Acceptance:** a portrait passes; a non-portrait is rejected in persona and the
session resets; either way the process stays alive.
**Rubric:** graceful handling of invalid input.

**Honest offline label fix** (`feature/2026-10-09-offline-verdict-labels`): the
key-free local (YuNet) fallback verdicts are labelled "(offline face check)" in
the chat, so an animal passing the weak offline face gate is never
misrepresented as a Gemini verdict. The detector only finds *faces* and cannot
discriminate animals — a documented limit; Gemini remains the real
discriminator. (Discrimination itself is out of scope: impossible offline.)

---

### 3. Interviewer — orchestrator

**Implemented** (`feature/2026-10-08-interviewer`): per-chat interview state
machine (`src/interview_state.py`, phases `idle → interviewing → complete`) with
a deterministic, key-free seven-question bank + rule-based animal matcher
(`src/interviewer.py`); the behavioural dossier accumulates per `chat_id` and
the finished profile + suggested animal hand off via the typed `UserProfile`
contract. Offline suite green; live-Telegram verification pending the user.

Sequential, stateful Q&A (5–7 questions, one at a time) accumulating a
behavioural dossier tied to `chat_id`, ending in a dossier summary plus a
suggested animal. Owns the state machine and dispatches to later stages.

**Acceptance:** questions are asked strictly one at a time; dossier accumulates
per `chat_id`; two concurrent users never see each other's state.
**Rubric:** multi-stage orchestration and session isolation.

---

### 4. Converter — hybrid portrait

**Implemented** (`feature/2026-10-08-converter`): one multimodal ADK call on
`gemini-3.1-flash-image` (`response_modalities=["IMAGE"]`) fuses the raw
portrait (`src/portrait_store.py`) with the interview dossier into a hybrid
animal portrait, returned **directly to Telegram with no intermediate text
hop** (`src/converter.py` + `send_photo`). ADK sessions are strictly per-call
(fresh delete-then-create + `finally` reap). Offline suite green (272 passed,
3 skipped — the 3 are guarded live-Gemini tests). **Key-blocked note:** the
local photo-booth composite (`src/local_composite.py`) is the live fallback
while `GEMINI_API_KEY` stays 403-blocked; the real Flash Image hybrid is
exercised by the guarded live test the moment a healthy key exists.

Gemini 3.1 Flash Image fuses the original photo with the interview dossier into
a hybrid animal portrait, returned **directly to Telegram with no intermediate
text hop**.

**Acceptance:** the chat receives an image; it was produced from *both* the
photo and the dossier; no stray text message between them.
**Rubric:** multimodal generation delivered to the user.

---

### 5. Scripter — narration

**Implemented** (`feature/2026-10-09-scripter`): after the profile text and
the hybrid photo, an ADK `LlmAgent` on `gemini-3.1-flash-lite` produces
**exactly one 60–90 word British-documentary paragraph** from the completed
`UserProfile` (`src/scripter.py`), delivered as the final chat message
(profile → photo → script) and stored raw on the shared state (`script`
field, schema v2) for the Phase 6 Narrator. A deterministic validator
(`validate_script`) is the single shape gate for both paths, with a key-free
local writer fallback (`src/local_script.py`). Offline suite green (342
passed, 4 skipped — the skips are guarded live-Gemini tests). **Key-blocked
note:** the key-free local writer is the live path today while `GEMINI_API_KEY`
stays 403-blocked; the real flash-lite paragraph is exercised by the guarded
live test the moment a healthy key exists.

Gemini 3.1 Flash Lite produces one dramatic British-documentary paragraph of
roughly 60–90 words built from the dossier.

**Acceptance:** output is a single paragraph within the word budget and is
grounded in the dossier rather than generic filler.
**Rubric:** content generation quality.

---

### 6. Narrator — TTS delivery

**Implemented** (`feature/2026-10-09-narrator`): the stored 60–90 word script
is routed **directly** — not as an agent — to `gemini-3.1-flash-tts-preview`
(`response_modalities=["AUDIO"]`, single-speaker prebuilt voice + persona
instruction, `src/narrator.py`), converted to Telegram-compatible OGG/Opus
when needed (ffmpeg seam, output format forced), and sent as a **voice note**
via `send_voice` — the fourth and final message (profile → photo → script →
voice). Audio is staged through temp files with `finally`-cleanup on success
and error; failures degrade to the locked `NARRATOR_REPLY_UNAVAILABLE`.
Offline suite green (403 passed, 5 skipped — the skips are guarded live-Gemini
tests). **Key-blocked note:** with `GEMINI_API_KEY` 403-blocked the real
TTS cannot be exercised live; there is deliberately **no local/fake TTS
fallback**, so the real voice-note path is proven by the guarded live test the
moment a healthy key exists.

**Acceptance:** the chat receives a playable audio note of the script.
**Rubric:** voice delivery completes the documentary.

---

### 7. Resilience & polish

`/restart` and `/start` reset (purge state **and** temp files, process keeps
running), wrong-payload-at-wrong-stage guards, API-timeout fallbacks, and the
logging/error policy from `TECH.md` applied throughout.

**Acceptance:** reset works from every phase; out-of-order text/media never
corrupts state; a Gemini timeout degrades gracefully with a logged, human-readable
message instead of a crash.
**Rubric:** reset behaviour and robustness under misuse.
