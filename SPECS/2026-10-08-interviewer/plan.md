# Phase 3 — The Interviewer: plan

TDD (Red/Green) repo. Run checks ONLY via the dev scripts
(`bash scripts/test`, `bash scripts/hooks` — see README).

## Task group 1 — Spec + branch (done)
- Branch `feature/2026-10-08-interviewer` cut from the Bouncer branch.
- `requirements.md`, `plan.md`, `validation.md` authored.

## Task group 2 — RED tests (unit): state driver + interviewer logic
- `tests/unit/test_interview_state.py`:
  - unknown chat → fresh `IDLE` state, `schema_version == 1`;
  - save/get round-trip; per-chat isolation (two chats never share state);
  - delete removes a chat (unknown again afterwards);
  - corrupt state (e.g. non-int chat_id / unknown phase string) → loud error.
- `tests/unit/test_interviewer.py`:
  - `start(chat)` → returns Q1 text, phase `INTERVIEWING`, question_index 1;
  - answering sequentially advances index and stores `(question, answer)`
    pairs in order;
  - questions are asked strictly one at a time (an answer returns exactly
    one question, never two);
  - an interview interrupted after Q3 resumes at Q4 on the next message;
  - after the 7th answer: phase `COMPLETE`, `completed=True`, profile present;
  - profile covers Habits/Quirks/Routines/Preferences and ends with a clearly
    identified `Suggested animal`;
  - animal matcher determinism: say a non-matching answer → locked default;
    matching keyword → its animal (locked pairs tested);
  - `reset(chat)` → fresh IDLE state again;
  - copy-lock: `INTERVIEW_QUESTIONS` has exactly 7 entries and its text is
    locked verbatim.

## Task group 3 — RED tests (component): gateway integration
- Extend `tests/component/test_gateway_interviewer.py` (new file; reuse the
  GateClient double pattern from `test_gateway_bouncer.py`):
  - text in `IDLE` → still exactly one "Hi Mate" (Bouncer regression);
  - approved photo sends the two Bouncer messages **and then starts the
    interview** (Q1 as third message);
  - text answers → one question per message, sequential, correct order;
  - finish → profile + animal reply;
  - two chats interleaved at different question indexes never cross state;
  - mid-interview `/restart` → reset + fresh start works;
  - rejected photo → rejection messages unchanged **plus** Interviewer state
    purged (next text is "Hi Mate", not a question continuation);
  - `interviewer=None` gateway → text always "Hi Mate" (backward compat);
  - any Interviewer exception during `answer/start` → loud log, loop survives.

## Task group 4 — Implement
- `src/interview_state.py`: `InterviewPhase`, `InterviewState`, `UserProfile`
  (Pydantic, typed), `InterviewStateStore` (in-memory dict + lock, one shared
  driver).
- `src/interviewer.py`: `INTERVIEW_QUESTIONS` (7 locked), animal matcher,
  profile builder, `Interviewer` class (ADK `LlmAgent` +
  `InMemorySessionService` per chat wired like Bouncer, `INTERVIEWER_MODEL`
  env override, `INTERVIEWER_REPLY_*` constants), `InterviewReply`.
- `src/gateway.py`: route text by `interviewer.state(chat).phase`; start
  interview on approved photo; `/start` `/restart` reset handling; purge
  Interviewer state on rejection; event logging
  (`event=interview_started`, `interview_answer_stored`,
  `interview_completed`, `interview_reset`).
- `main.py`: construct store + `Interviewer`, pass into `Gateway`.

## Task group 5 — Green
- `bash scripts/test` then `bash scripts/hooks` — all offline suites green,
  existing 2 live-Gemini skips remain skipped.

## Task group 6 — Live Telegram check (with user)
- Restart the supervised bot, send a person photo → interview starts; answer
  all 7 → profile + suggested animal; `/restart` → fresh start; send an
  animal photo mid-flow → rejection + state purge.

## Task group 7 — Docs + PR
- Sync `README.md` bot-behaviour table and
  `SPECS/2026-10-08-interviewer/validation.md` with what was implemented.
- Commit on `feature/2026-10-08-interviewer`, open PR #3 against `main`
  (expect PR #2 commits in its diff — Bouncer unmerged), report URL.