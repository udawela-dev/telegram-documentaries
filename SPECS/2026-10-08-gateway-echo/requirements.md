# Requirements: Repository & gateway (Phase 1)

## Context
This is Phase 1 of the Telegram Documentaries roadmap (ROADMAP.md). The goal is minimal scaffolding: project skeleton, .env loading, and a long-polling loop that dispatches updates and replies with a hardcoded message.

This spec implements only what is listed. No Gemini/LLM, no state machine beyond minimal tracking, no database, no webhooks.

## User's exact requirement
"Generate the entrypoint based on the specs. When I send a message to the Telegram bot on my phone the bot should reply with a hardcoded message such as 'Hi Mate' (no API calls just yet). Every time we send a message, the same hardcoded message should be sent back."

## Scope
In scope:
- Python project skeleton for Phase 1
- Load secrets from .env (TELEGRAM_BOT_TOKEN at minimum; GEMINI_API_KEY may be present but not used)
- Raw HTTP long polling against Telegram Bot API `getUpdates` endpoint (no python-telegram-bot, no google-adk telegram integration)
- Polling loop that receives updates, dispatches them, and replies to each applicable message with hardcoded string "Hi Mate"
- Same hardcoded message on EVERY message received (no content-based branching, no state/memory)
- Offset-based update tracking so each update is processed only once (survive failures without reprocessing the same update inappropriately)
- Typed Pydantic models at boundaries for Telegram updates (parse raw JSON into validated models before use). Treat external input as untrusted; skip malformed/unexpected updates with logging.
- Comprehensive structured logging with decorators where sensible (do not mix business logic with logging). No bare except:pass, no swallowed exceptions, no un-logged fallbacks. Fail loudly and log for non-critical work.
- Dependencies pinned in requirements file; installable via pip
- scripts/ directory with scripts/test (ground truth for tests)
- README.md documenting setup, env vars, how to run, what scripts/test does
- Entry point runnable as `python main.py`

Out of scope (explicit):
- Gemini/LLM calls, GEMINI_API_KEY usage beyond loading if present
- Bouncer, Interviewer, Converter, Scripter, Narrator
- State machine phases beyond Phase 1 minimal flow
- In-memory session keyed by chat_id beyond what’s needed for nothing (no state)
- Webhooks, public URLs, persistence, media generation, TTS, groups, restart semantics beyond what exists naturally (Phase 7 covers full restart)
- Any future features

## Locked decisions
1. Transport: RAW HTTP long polling against `getUpdates`. Library: httpx OR requests. Pick one, justify briefly in implementation/docs. No bot frameworks.
2. Reply is HARDCODED string "Hi Mate". Zero LLM calls.
3. Same hardcoded message on EVERY message — no state, no memory, no branching on message content.
4. Language: Python 3.11
5. Environment: no virtualenv tooling assumed; dependencies in pinned requirements file, installable via pip

## Technical contracts (from TECH.md)
- Secrets from .env: TELEGRAM_BOT_TOKEN, GEMINI_API_KEY. Never print/log/commit token.
- Typed Pydantic models at boundary: parse raw Telegram update JSON into validated model BEFORE use. Never pass raw dicts across module boundaries. Treat all external input as untrusted/arbitrary (may lack message/text/chat, be inline edits/channel posts, malformed). Malformed/unexpected updates must be skipped with a log, never crash the loop.
- Comprehensive structured logging via DECORATORS where sensible. No bare except:pass, no swallowed exceptions, no un-logged fallbacks. Fail loudly and log for non-critical work.
- Long polling correct: use offset/offset accumulation so same update never processed twice; survive individual update failures without dying.
- Repo hygiene: .env in .gitignore (done), reproducible pinned dependencies.
- Testing: RED/GREEN TDD — tests written BEFORE code. scripts/test (and scripts/hooks if configured) ground truth. Check .opencode/ for existing hook/lint config before prescribing tools.
- README.md must document setup, env vars, how to run bot, what scripts/test does.
- Entry point runnable as `python main.py` (or declare exact run command).

## Design principles
- Simple, elegant, general within Phase 1 scope. YAGNI strictly.
- Decorator-based logging to avoid mixing business logic with logging.
- Prefer typed schemas over regexes.
- Fail loudly and log for non-critical work; never silently swallow exceptions.
- Isolate boundary parsing; internal code works with typed values.

## Acceptance (from ROADMAP Phase 1)
- scripts/test passes
- Bot receives update over long polling and replies
- No webhook, no public URL required

## Ambiguities — resolved during implementation
- HTTP library: **httpx** (sync) — chosen over requests for injectable
  MockTransport, modern timeouts, sync+async. Documented in `src/telegram_client.py`.
- Test framework: **pytest** — run via `scripts/test`.
- Logging: **stdlib logging** with decorator helpers + redaction filter
  (`src/logging_utils.py`) — no heavyweight dependency.
- Minimal Pydantic models: `Update`/`Message`/`Chat` + `parse_updates` — only
  `update_id`, `chat.id`, `message`, `text`; unknown fields tolerated
  (`extra="allow"`, `strict=True`).
