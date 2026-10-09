# Phase 3 — The Interviewer (stateful orchestrator)

## Context

Pipeline (ROADMAP): Telegram → **Bouncer** (Phase 2, photo gate) → **Interviewer**
(Phase 3, this feature) → Converter (Phase 4, synthesizes human + suggested
animal) → Scripter → TTS.

Today text messages always get a static "Hi Mate"; the photo gate (Bouncer) is
complete and merged-ready on its own PR. Phase 3 turns the chat into a real
per-chat interview state machine.

Decisions confirmed with the user (2026-10-08):

1. New feature branch `feature/2026-10-08-interviewer`, PR #3 against `main`.
2. **Curated question bank + local profile/animal builder.** Gemini is
   API-key-blocked (403) until a healthy key exists, so the agent must be fully
   functional in Telegram today. An ADK `LlmAgent` on `gemini-3.1-flash-lite`
   (with an `InMemorySessionService` session per chat) is wired as the agent
   backbone exactly mirroring The Bouncer, but question selection and profile
   generation are deterministic, offline, and testable.
3. `/start` and `/restart` commands purge a chat's Bouncer session + Interviewer
   state (TECH.md reset semantics; needed to re-test the loop in Telegram).

## Scope

- A per-chat interview state machine driver: progress + answers keyed by
  `chat_id` (int), versioned Pydantic schema, in-memory, one shared driver
  owning all reads/writes.
- **The Interviewer** agent: asks **7** playful, investigative, documentary-style
  questions **one at a time**; each answer is stored in order before the next
  question is asked; an interrupted interview resumes at the right question.
- After the 7th answer, a **behavioural/personality profile** covering habits,
  quirks, routines and preferences is built, plus a clearly identified
  **suggested animal** for the next stage (Converter).
- The completed profile is passed to the next pipeline stage as a typed
  contract: stored on the shared state driver, emitted as a
  `event=interview_completed` log record, and (until the Converter exists) the
  gateway replies with the profile + suggested animal.
- Gateway integration: approved photo → interview starts (Q1); text answers
  advance the interview; `idle` text still gets "Hi Mate" (confirmation flow
  unchanged); `/start` `/restart` reset; rejected photo purges Interviewer
  state too (mirrors the existing reset); Bouncer behaviour untouched.
- Tests: unit + component. Live Telegram verification (sequential questions,
  stored answers, final profile + animal).

## Out of scope (YAGNI)

- No persistence beyond the in-memory driver (TECH.md).
- No Converter/Scripter/TTS integration yet — only the profile contract and its
  hand-off event are produced.
- No per-user LLM variety in questions (user chose the curated bank).

## Locked decisions (copy-locked by tests)

- Question bank: exactly 7 locked questions in `src/interviewer.py`
  (constants `INTERVIEW_QUESTIONS`), playful + slightly eccentric documentary
  style, asked strictly sequentially.
- Profile output labels: `Habits` / `Quirks` / `Routines` / `Preferences` /
  `Suggested animal: X` — the "Suggested animal" must be clearly identified.
- Suggested animal: deterministic rule-based matching over the stored answers
  (known keyword families → canonical animal); falls back to a locked default
  when nothing matches; fully unit-tested.
- `chat_id` is typed `int` everywhere (matches `telegram_models`; string-vs-int
  drift would silently break state lookups — a named category).
- State schema is versioned (`schema_version: int = 1`); a missing state for a
  chat means "fresh state" (no exception); a corrupt/foreign type is a loud
  error, never silent.
- Business logic never lives in log statements (decorator-style separation;
  event logging from callers/module functions).
- Illegal state transitions raise (fail loud), never silently mutate.

## Contracts

```python
# src/interview_state.py
class InterviewPhase(str, Enum):
    IDLE = "idle"            # awaiting a photo (gate) — text gets "Hi Mate"
    INTERVIEWING = "interviewing"  # awaiting the answer to question N
    COMPLETE = "complete"    # profile produced; kept for the next stage

class InterviewState(BaseModel):
    chat_id: int
    phase: InterviewPhase
    question_index: int = 0             # 0-based, next question to ask
    answers: list[tuple[str, str]] = [] # (question, answer) in order
    profile: UserProfile | None = None
    schema_version: int = 1
    updated_at: str                     # ISO-8601 UTC

class UserProfile(BaseModel):
    chat_id: int
    summary: str            # full profile text incl. Habits/Quirks/Routines/Preferences
    suggested_animal: str   # clearly identified animal for the Converter

class InterviewStateStore:
    get(chat_id) -> InterviewState      # unknown -> fresh IDLE state
    save(chat_id, state) -> None
    delete(chat_id) -> None

# src/interviewer.py
class Interviewer:
    start(chat_id) -> str               # -> Q1 (transitions IDLE->INTERVIEWING)
    answer(chat_id, text) -> InterviewReply  # stores answer; next Q or profile
    # InterviewReply: .messages: list[str], .completed: bool, .profile: UserProfile|None
    state(chat_id) -> InterviewState
    reset(chat_id) -> None
```

Gateway: `Gateway(client, bouncer=..., interviewer=None)` — `None` means "text
always says 'Hi Mate'" (backward-compatible default). With an interviewer: text
routing depends on `interviewer.state(chat_id).phase`.

## Reset semantics

- `/start` or `/restart` (any message whose text starts with the token):
  `bouncer.reset_chat(chat_id)` + `interviewer.reset(chat_id)` + confirmation
  reply. Never fails the loop; reset failures log loudly.
- Rejected photo: existing rejection flow + `interviewer.reset(chat_id)`.
- `IDLE` text (not a command): "Hi Mate" (unchanged).
- `COMPLETE` text (not a command): re-send the stored profile + animal summary.