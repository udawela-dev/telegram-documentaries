# Phase 5 — The Scripter: plan

TDD (Red/Green) repo. Run checks ONLY via the dev scripts
(`bash scripts/test`, `bash scripts/hooks` — see README).

## Task group 1 — Spec + branch (done)
- Branch `feature/2026-10-09-scripter` cut from the Converter tip
  (`62c9d46` on `feature/2026-10-08-converter`, PR #4; PR stack: not main).
- `requirements.md`, `plan.md`, `validation.md` authored.

## Task group 2 — RED tests (unit)
- Extend `tests/unit/test_interview_state.py`:
  - `script` defaults to `None` on a fresh/IDLE state;
  - `schema_version == 2` by default;
  - a stored v1 record (constructed with `schema_version=1`) is **migrated**
    losslessly to v2 (`script=None`) on `get`/`save`, with `event=state_migrated`;
  - a record with `schema_version > 2` fails loud (`InterviewStateError`).
- `tests/unit/test_local_script.py`:
  - deterministic (identical profile → identical bytes/str);
  - exactly one paragraph, 60–90 words, no markdown — asserted for the default
    animal *and* every canonical animal (`the Night Owl`, `the Early Bird`,
    `the Mountain Goat`, `the Sea Otter`, `the Social Butterfly`,
    `the Lone Wolf`, `the Busy Bee`, `the Bookworm`, `the Dormouse`,
    `the House Cat`);
  - grounded: the suggested animal appears in the script.
- `tests/unit/test_scripter.py` (mirror `tests/unit/test_converter.py`):
  - `validate_script`: 70-word single paragraph → normalized text; empty;
    59 and 91 words → `None`; two paragraph blocks → `None`; markdown
    (`**bold**`, bullet, backtick, heading, horiz. rule) → `None`;
  - prompt assembly: one `Content` whose text embeds `profile.summary` and
    `profile.suggested_animal` and the locked shape requests (one paragraph /
    60–90 words / no markdown);
  - `_run_llm` seam: a scripted valid paragraph flows through `write_script`,
    gets stored on the shared state (`state.script == script`, phase/profile
    untouched), and the ADK session is fresh-then-reaped (single call);
  - LLM failure + local writer wired → local used (`event=script_generated
    source=local`), stored;
  - LLM returns a shape-rejected script + local writer wired → local used,
    `event=script_validation_failed` logged, no off-spec text stored;
  - no local writer + failing LLM → `ScripterError`, state untouched
    (`script is None`);
  - no key in view + local writer → no LLM round-trip, straight to local;
  - timeout (bounded, `SCRIPTER_GEMINI_TIMEOUT`) → local fallback, within budget;
  - `resolve_model` env override; defaults (`gemini-3.1-flash-lite`);
  - copy-lock `SCRIPTER_REPLY_UNAVAILABLE` and `SCRIPTER_MIN_WORDS`/`MAX_WORDS`.

## Task group 3 — Implement
- `src/interview_state.py`: add `script: str | None = None`; `schema_version`
  default → `2`; shared `_normalize`/migration helper used by `get` and `save`
  (v1 → v2 lossless, logged; newer-than-current loud).
- `src/local_script.py`: `LocalScriptWriter.write(profile)` — deterministic
  one-paragraph documentary template grounded in the suggested animal.
- `src/scripter.py`: `Scripter` (ADK `LlmAgent` on `gemini-3.1-flash-lite`,
  per-call fresh+reaped sessions, `SCRIPTER_MODEL` override, `_run_llm` seam,
  `validate_script`, bounded timeout, local fallback, `write_script` persists
  via the shared store, `ScripterError`, `SCRIPTER_REPLY_UNAVAILABLE`).
- `src/gateway.py`: `scripter` constructor param; `_handle_scripting`
  (`scripter_started`, `script_stored`, `script_failed`) invoked after
  `event=hybrid_sent` — on success it `send_message`s the validated script text
  to the same chat (chat-visible order: profile text → hybrid photo → script
  text) and the Scripter stores the raw string on the driver; missing profile /
  scripter failure → loud + graceful `SCRIPTER_REPLY_UNAVAILABLE`;
  `scripter=None` → Phases 3+4 unchanged.
- `main.py`: build `LocalScriptWriter()` + `Scripter(api_key=...,
  store=interview_store, local_writer=...)` on the shared store, wire into the
  `Gateway` (logged `event=scripter_ready`).

## Task group 4 — RED tests (component)
- `tests/component/test_gateway_scripter.py` (GateClient + a `FakeScripter`,
  house style):
  - full interview + converter + scripter → the **full chat-visible ordering is
    asserted via `GateClient.sent`: profile message → hybrid photo → script
    message** (`calls[-3] == message(profile)`, `calls[-2] == photo`, `calls[-1]
    == message(script)`), and the raw script is stored on the shared state
    (`interviewer.state(chat).script == script`);
  - scripter failure → `SCRIPTER_REPLY_UNAVAILABLE` after the photo, loud
    `event=script_failed`, `state.script is None`, loop survives (next update
    processed);
  - `scripter=None` → Phases 3+4 behaviour unchanged (photo still sent, no
    scripter events, `script is None`) — regression;
  - converter failure → scripter not invoked (`scripter.calls == []`);
  - `converter=None` → scripter not invoked;
  - `/restart` after a stored script purges it (`state.script is None`).
- `tests/integration/test_live_scripter.py` (guarded: skips unless
  `RUN_LIVE_GEMINI=1` and a healthy key — style of
  `tests/integration/test_live_converter.py`): a real
  `gemini-3.1-flash-lite` `write_script` on a seeded store, built **without** a
  local writer, returns a valid one-paragraph 60–90 word script grounded in the
  suggested animal (a genuine live-LLM proof).
- Phases 1–4 suites keep passing unchanged.

## Task group 5 — Green
- `bash scripts/test` then `bash scripts/hooks` — all offline suites green;
  the guarded live tests (Bouncer ×2, Converter ×1, Scripter ×1) stay skipped
  without `RUN_LIVE_GEMINI=1` + a healthy key.

## Task group 6 — Live Telegram check (with user)
- Restart the supervised bot. User sends a person photo, completes the 7
  questions: expect **profile text → hybrid photo → the script text message
  arriving in the same chat**, the bot logging `event=script_generated
  source=local` + `event=script_stored` (today the key-free local writer, since
  the Gemini key is 403-blocked) and no `SCRIPTER_REPLY_UNAVAILABLE`. The real
  `gemini-3.1-flash-lite` paragraph is exercised by the guarded live test the
  moment a healthy key exists. `/restart` spot-check clears the stored script.

## Task group 7 — Docs + PR
- Sync README (current state, behaviour table "after hybrid photo → the
  narration is scripted and stored for the voice note", env knobs
  `SCRIPTER_MODEL` / `SCRIPTER_GEMINI_TIMEOUT`, architecture line, live-test
  command incl. `test_live_scripter.py`) and `validation.md` to what was
  implemented; TECH.md session-state (script field, schema_version 2) and
  key-free resilience (local script writer) notes; ROADMAP Phase 5 marked
  Implemented with the key-blocked honest note.
- Commit on `feature/2026-10-09-scripter`, open PR #5 against `main`, report
  the URL and remaining issues (Gemini key still blocked → real LLM paragraph
  unexercised live).