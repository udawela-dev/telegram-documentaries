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

Gemini 3.1 Flash Image fuses the original photo with the interview dossier into
a hybrid animal portrait, returned **directly to Telegram with no intermediate
text hop**.

**Acceptance:** the chat receives an image; it was produced from *both* the
photo and the dossier; no stray text message between them.
**Rubric:** multimodal generation delivered to the user.

---

### 5. Scripter — narration

Gemini 3.1 Flash Lite produces one dramatic British-documentary paragraph of
roughly 60–90 words built from the dossier.

**Acceptance:** output is a single paragraph within the word budget and is
grounded in the dossier rather than generic filler.
**Rubric:** content generation quality.

---

### 6. Narrator — TTS delivery

Route the script directly to `gemini-3.1-flash-tts-preview`, render to a
Telegram-compatible audio format (OGG/MP3), and send it as a voice note. **Not an
agent** — no reasoning step.

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
