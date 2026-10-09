# Plan — Phase 7: Resilience & polish (resets + edge-case hardening)

Red/Green TDD: tests are written **before** the code they specify. Run
`scripts/test` and `scripts/hooks` after each task group. Inspect
`src/gateway.py` (poll/dispatch/reset), `src/interview_state.py`,
`src/portrait_store.py`, and the stage modules before editing; reuse the
established per-`chat_id` dict + lock + typed-contract patterns
(PortraitStore, InterviewState driver) rather than inventing parallel
machinery.

## Task group 1 — Temp-asset registry

- New `src/temp_assets.py`: `TempAssets` (thread-safe, `chat_id:int` keys) with
  `track(chat_id, path)`, `purge(chat_id)`, `TempAssetsError` for wrong-typed
  input; `purge` unlinks each tracked path, removes entries, logs each failure
  loud but never raises to the caller.
- Tests first: track/purge happy path; purge of already-missing file is
  logged-and-continues; wrong-typed `chat_id` raises; two chats' assets never
  cross-purge.
- Wire stage modules that stage temp files (Narrator `.wav/.ogg`, Converter
  composite/photo, Bouncer none, Scripter none) to `track()` at creation.
  `finally`-cleanup stays the primary path.

## Task group 2 — Sequential dispatch + stale-task invalidation

> **Review-closed (implemented):** the originally planned epoch guard is dead
> code by construction — dispatch is strictly single-threaded and every stage
> call blocks `poll_once` up to its timeout, so a reset can never interleave
> mid-generation. Per user decision (Option 2) the guard was **retired**; the
> guarantee is instead documented and regression-tested as sequential dispatch.

- `_handle_reset_command` runs the full purge for the requesting chat: Bouncer
  session, Interviewer state, portrait, Converter session, Scripter session,
  registered temp assets. No epoch bump needed.
- `poll_once` processes each update fully before the next — document the
  sequential contract in the class (a reset always lands at the next free step;
  stage timeout workers are daemons that cannot send or write state).
- Also drop in-batch **duplicate Updates** (same `update_id` twice): skip and
  log `event=skip_duplicate_update`.
- Tests first: reset behind a stage-driving update in the same batch delivers
  the in-flight step's outputs and then purges at the next free step
  (`test_reset_queued_behind_a_stage_update_is_honoured_at_the_next_free_step`);
  two duplicate Updates in one batch apply only once.

## Task group 3 — Wrong payload at wrong stage

- `_dispatch`: route non-photo media (no `message.text`) to a small
  `_handle_media` path — log `event=media_no_photo`, then the same
  phase-appropriate reply as text.
- `_handle_photo`: query the interview phase first; if not `IDLE`, reply
  `GATEWAY_REPLY_PHOTO_DURING_INTERVIEW` (photo during `COMPLETE` too) and
  return without touching the Bouncer, portrait store, or interview state.
- `_handle_text` while `IDLE`: replace the "Hi Mate" confirmation with
  `GATEWAY_REPLY_NEED_PHOTO` (log `event=idle_text_photo_prompt`).
- New locked constants `GATEWAY_REPLY_NEED_PHOTO`,
  `GATEWAY_REPLY_PHOTO_DURING_INTERVIEW`, retry hint `GATEWAY_REPLY_RETRY_HINT`
  in `src/gateway.py` copy block.
- Tests first: photo mid-interview never calls `bouncer.classify` (spy) and
  never overwrites the portrait; text at `IDLE` gets the photo prompt; media
  (video/document) gets `media_no_photo` log + phase reply; regression tests
  for previous "Hi Mate" behaviour are updated intentionally.

## Task group 4 — Retry hints on Gemini failures + full reset sweep

- After every degraded stage reply (Bouncer unavailable, Converter
  unavailable, Scripter unavailable, Narrator unavailable, photo download
  failure), send `GATEWAY_REPLY_RETRY_HINT` once and log the diagnostic
  (stage + error type + chat + update).
- `_handle_reset_command`: add `scripter.reset_chat` + `TempAssets.purge(chat_id)`;
  keep per-stage try/except (never fatal); `INTERVIEWER_REPLY_RESET` remains the
  fresh welcome; the chat may begin again immediately (next photo re-enters at
  `IDLE`).
- Tests first: reset purges scripter session + registered temp files; a purge
  failure logs loud and the loop keeps replying; reset during each stage phase
  (`IDLE`/`INTERVIEWING`/`COMPLETE`) ends `IDLE` with no old sends afterwards;
  two chats stay isolated under interleaved resets.

## Task group 5 — End-to-end and docs

- New `tests/integration/test_reset_end_to_end.py`: drives the real gateway +
  stages with a recording fake Telegram client — full portrait → interview →
  converter → scripter → narrator run (stage fallbacks or live Gemini as the
  environment allows), then `/restart`, then a second complete run **without
  restarting the process**; asserts old files/answers/scripts are gone and the
  new run is clean.
- Update `SPECS/ROADMAP.md` Phase 7 (mark implemented + branch/notes),
  `SPECS/TECH.md` (reset semantics: sequential dispatch, temp-asset registry,
  media routing, duplicate-guard), and `README.md` (documented `/start` +
  `/restart` behaviour) — the verifier confirms the final wording after
  implementation.
- Run `scripts/test`, then `scripts/hooks`; no claims of passing without
  actual runs.