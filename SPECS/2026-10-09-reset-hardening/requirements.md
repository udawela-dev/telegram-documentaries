# Requirements — Phase 7: Resilience & polish (resets + edge-case hardening)

Date: 2026-10-09. Branch: `feature/2026-10-09-reset-hardening` (stacked on PR #7).

## Context

ROADMAP Phase 7 — "Resilience & polish". The gateway already dispatches
`/start`/`/restart` (`src/gateway.py:188`, `_handle_reset_command`) and resets
the Bouncer session, Interviewer state, portrait store, and Converter session,
but several gaps remain between the contract (TECH.md "Reset semantics") and
actual behaviour:

1. **Scripter session is never purged on reset** — `_handle_reset_command` does
   not call `scripter.reset_chat` (the method exists, idempotent).
2. **Stale async tasks fire after a reset.** Bouncer/Converter/Scripter/Narrator
   run Gemini work in daemon threads with Event timeouts. A thread that times
   out (or is still mid-flight) when `/restart` arrives may finish later and
   still send its result / stage temp files — corrupting the fresh run.
3. **Photo during an interview re-runs the Bouncer.** `_dispatch` routes every
   photo to `_handle_photo` regardless of phase, so a second photo mid-interview
   re-classifies, re-saves the portrait, and restarts Q1 over the active
   interview.
4. **Text during the Bouncer stage** (interview phase `IDLE`) gets the Phase-1
   "Hi Mate" confirmation instead of a "send a clear portrait photo" prompt.
5. **No in-batch duplicate-update guard.** The offset prevents Telegram
   redelivery, but two identical `Update`s inside one batch would double-advance
   the state machine.
6. **Stage failure replies do not offer a retry**, and non-photo/non-text media
   types are not distinguished or logged.
7. **No per-chat registry of staged temp assets**, so a reset cannot sweep
   orphaned media left by an interrupted worker.

## Scope

Deliver Phase 7 only. Preserve the existing Bouncer, Interviewer, Converter,
Scripter, and Gemini TTS stages — no restructuring of the pipeline, no new
stages, no persistence layer, no process restart.

### In scope

- **Commands.** `/start` and `/restart` are **identical** (per user decision):
  instant, no confirmation prompt (per user decision), per-`chat_id` reset.
- **Reset purges, for the requesting chat only**:
  - Bouncer ADK session, Interviewer state (phase → `IDLE`, dossier → None),
    Converter ADK session, **Scripter ADK session** (new), portrait bytes,
    and **registered temp assets** (see Temp-asset registry).
- **Stale-task invalidation by construction.** Dispatch is **strictly
  sequential** (single-threaded `poll_once`; every stage call blocks up to its
  timeout), so a reset is always honoured at the **next free step** and can never
  interleave mid-generation. Stage timeout workers are daemon threads that can
  neither send to Telegram nor write shared state. No in-flight result can
  outlive a reset — no epoch token needed. (Review-closed: the originally
  specified epoch guard is **unreachable code** in this architecture and was
  retired; regression test `test_reset_queued_behind_a_stage_update_is_honoured...`
  locks the sequential guarantee.)
- **Wrong payload at wrong stage.**
  - Photo while `INTERVIEWING` (or any active pipeline stage after `IDLE`):
    reply "please answer the current question with text, or type /restart";
    the Bouncer must **not** run again and the portrait must not be overwritten.
  - Text (and non-photo media) while `IDLE` (the Bouncer stage): reply "upload a
    clear portrait photo".
  - Media types that are neither photo nor text (video, audio, document,
    sticker): logged distinctly (`event=media_no_photo`), replied per phase with
    the same copy as text.
- **Gemini failure handling (vision / image / TTS).** Catch at each stage
  boundary, log loud + diagnostic (stage, error type, chat, update), notify the
  user with the existing locked degraded replies, then send a locked **retry
  hint** — e.g. "try again, or type /restart" — without crashing polling.
- **In-batch duplicate updates.** Skip any `Update` whose `update_id` is not
  strictly greater than the highest already processed in this batch
  (log `event=skip_duplicate_update`).
- **Temp-asset registry.** New `src/temp_assets.py`: `track(chat_id, path)`,
  `purge(chat_id)`, chat-scoped, thread-safe, typed; staged temp files are
  registered at creation; `finally`-cleanup remains the primary path and the
  registry is the sweep-net that `/restart` uses; purge failures are logged
  loud, never fatal to the loop.
- **Chat isolation.** Everything above is strictly per `chat_id`; tests prove
  two chats never observe each other's reset.

### Out of scope (YAGNI)

- Confirmation prompts, `/cancel`, global reset, webhook mode, persistence,
  cross-chat operations, restarting the Python process, changing stage
  behaviour (fallbacks, models, voices), new env vars.

## Contracts

- **Sequential dispatch contract.** `poll_once` processes one update fully
  before the next; stage handlers are synchronous with bounded timeouts. A
  reset queued behind an in-flight-step update is processed strictly after it
  and purges everything for that chat.
- **Temp-asset registry** is the single typed boundary for on-disk media owned
  by a chat: `track(chat_id: int, path: Path)` / `purge(chat_id: int)`; a
  wrong-typed `chat_id` raises `TempAssetsError` (fail loud).
- **Replies** use the existing locked constants plus two new locked copies:
  `GATEWAY_REPLY_PHOTO_DURING_INTERVIEW` and `GATEWAY_REPLY_NEED_PHOTO`, and a
  shared retry hint `GATEWAY_REPLY_RETRY_HINT`. Never a raw traceback in chat.

## Decisions (user-confirmed)

1. `/start` and `/restart` do exactly the same thing (reset + welcome).
2. Reset is instant, **no** confirmation prompt.
3. Branch stacks on PR #7 (`feature/2026-10-09-offline-verdict-labels`).
4. No backward-compat concerns: prior behaviour (photo re-gate mid-interview,
   "Hi Mate" on idle text) is intentionally replaced; update affected tests.