# Validation — Phase 7: Resilience & polish (resets + edge-case hardening)

All offline items must pass via the project's own scripts
(`scripts/test`, `scripts/hooks`); every item below is ticked only on
**actual** execution, never on intent.

## Offline acceptance (CI-runnable)

- [x] `scripts/test` green (new unit + component + integration tests included).
- [x] `scripts/hooks` green (test + lint + type checks).
- [x] Unit: temp registry purges tracked assets per chat; failures logged, not
      raised; wrong-typed input raises `TempAssetsError`.
- [x] Unit: sequential dispatch — a `/restart` queued behind a stage-driving
      update in the same batch is honoured at the next free step (the in-flight
      step's outputs are delivered, then the purge runs); stage timeout workers
      cannot send or write state (`test_reset_queued_behind_a_stage_update_is_honoured...`).
      The epoch guard is retired (review-closed; unreachable by design, user
      decision).
- [x] Unit: photo during `INTERVIEWING`/`COMPLETE` → `GATEWAY_REPLY_PHOTO_DURING_INTERVIEW`,
      Bouncer `classify` not invoked (spy), portrait not overwritten.
- [x] Unit: text and non-photo media at `IDLE` → `GATEWAY_REPLY_NEED_PHOTO`
      (+ `media_no_photo` log for media).
- [x] Unit: duplicate `Update`s in one batch apply once
      (`skip_duplicate_update`) — the interview advances exactly one step.
- [x] Component: `/restart` during each phase (`IDLE`/`INTERVIEWING`/`COMPLETE`)
      returns the chat to `IDLE`, purges portrait + scripter/converter/bouncer
      sessions + temp assets, and sends the fresh welcome; two chats reset in
      interleaved order never observe each other's state.
- [x] Integration: full run → `/restart` → second full run in one process; the
      second run starts clean (no old answers/script/audio) and no stale task
      output leaks across the reset (test file described in plan.md).

## Live acceptance (needs the running bot + healthy key + a Telegram user)

- [ ] Start a real run in the user's chat: photo → 7 answers → profile text →
      hybrid photo → script → **playable voice note**.
- [ ] Mid-interview (while a question is pending), type `/restart`: the chat
      is reset instantly to the greeting; the interview resumes cleanly from a
      fresh photo; no late voice note/photo from the aborted run ever arrives.
- [ ] Send a photo while a question is pending → the bot asks to answer with
      text or `/restart` (and does not restart the interview on its own).
- [ ] Send plain text before any photo → the bot asks for a clear portrait
      photo.
- [ ] Trigger a Gemini failure path (if reachable) → degraded reply + retry
      hint; polling continues (`polling_started` still logged; next message is
      answered).
- [ ] Confirm old files/answers/scripts/audio are gone after the reset and the
      bot keeps polling after every invalid input above.

## Spec drift

- [ ] `ROADMAP.md` Phase 7, `TECH.md` (reset semantics), and `README.md`
      updated to reflect what was actually implemented; spec files corrected
      on any drift, after user approval.