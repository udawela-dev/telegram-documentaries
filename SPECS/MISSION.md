# MISSION.md — The Telegram Documentaries

## Vision

Send a selfie to a Telegram bot, answer a short interview about yourself, and
receive back a narrated comedy wildlife documentary about *you*: a hybrid
animal portrait of your face plus a dramatic British-documentary voice note
describing your species in the wild.

**End-to-end user experience:**

1. User sends `/start`, then uploads a portrait photo.
2. **The Bouncer** inspects it. A human face is waved through; anything else is
   cheekily rejected and the session resets.
3. **The Interviewer** asks 5–7 questions, one at a time, and builds a
   behavioural dossier keyed to the user's `chat_id`.
4. **The Converter** merges the original photo with the dossier into a hybrid
   animal portrait and sends it straight back to the chat.
5. **The Scripter** writes a single dramatic narration paragraph.
6. **The Narrator** renders that script as a Telegram voice note.
7. User may send `/restart` at any point to begin again.

## In scope

- The five pipeline stages: **Bouncer → Interviewer → Converter → Scripter →
  Narrator**.
- Telegram **long polling** transport (no webhooks, no public URL).
- In-memory session state keyed by `chat_id`.
- `/start` and `/restart` that purge session state *and* temporary media without
  restarting the process.
- Secrets loaded from `.env`: `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`.

## Out of scope

- **Webhooks** — long polling only. No public IP, tunnel, or SSL certificate.
- **Persistence** — no database, no disk-backed session store. State lives in
  memory and is lost when the process stops.
- **Video, music, image albums** — the outputs are exactly one image and one
  audio note.
- **Group chats and non-English locales** — direct 1:1 chats, English only.
- **Admin / moderation commands, inline keyboards** — text and media replies only.
- **The Bouncer does not narrate or generate media** — it is a text/image
  classification gate with no TTS and no video.
- **Any hypothetical future feature** — if it is not in this file or the
  roadmap, it is not being built.

## Success criteria

A run is successful when all of the following hold:

1. **Happy path** — a valid portrait produces, in order: a passed gate, the
   full interview, a hybrid portrait image, and a playable voice note.
2. **Reset** — `/restart` at *any* stage clears the dossier and deletes
   temporary media, then the next upload starts cleanly at the Bouncer. The
   process never restarts.
3. **Out-of-order input** — text sent while an image is expected (or an image
   sent while text is expected) is handled gracefully: the user is told what the
   bot is waiting for, state is not corrupted, and the conversation continues.
4. **Non-human input** — a non-portrait image is rejected with in-persona
   humour and the session resets, rather than crashing or proceeding.

## Non-negotiables

- **Never leak another user's session.** State is strictly per-`chat_id`; one
  user's dossier, photo, or script must never reach another chat.
- **Never hardcode or commit secrets.** `.env` stays out of version control;
  only `.env.example` (placeholders) is committed.
- **Never swallow an error silently.** Every failure is logged.
