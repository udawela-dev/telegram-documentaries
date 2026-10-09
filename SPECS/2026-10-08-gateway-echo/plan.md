# Plan: Repository & gateway (Phase 1)

This is a Red/Green TDD plan. Tests are written BEFORE code. All checks run via scripts/ (ground truth).

Status: **COMPLETE** — implemented on branch `feature/2026-10-08-gateway-echo`.

Implementation decisions (documented in code):
- HTTP lib: **httpx** (sync) — injectable `MockTransport` for tests, modern timeouts.
- Test framework: **pytest** run via `scripts/test`; `scripts/hooks` = `compileall` + tests.
- Logging: stdlib `logging` + `src/logging_utils.py` (structured `event=` records,
  `log_call` decorator, `SecretRedactionFilter`, `configure_logging`).
- Boundary models: `src/telegram_models.py` (`Chat`/`Message`/`Update` + `parse_updates`
  with skip-malformed semantics; `extra="allow"` + `strict=True`).
- Errors: `TelegramAPIError` (API/network) and `MalformedResponseError` (bad shape);
  tokens redacted from every raised error and every log record.
- Gateway: `offset` starts at 0 (first poll sends no offset param), advances to the
  highest `update_id` + 1 after each batch; `poll_once()` returns the number of
  replies sent; `run(stop_event)` paces with backoff, logs `event=polling_started/stopped`,
  and survives poll/update/send failures.
- Integration tests run against a **local HTTP stub** (no live API, no network needed).

## Task 1: Project skeleton
- [x] Create base directories: `src/`, `tests/`, `scripts/` (if not exist)
- [x] Create `requirements.txt` with pinned deps: pydantic (for models), python-dotenv (env loading), and chosen HTTP lib (httpx or requests). Also test deps (pytest). Pin minimal versions.
- [x] Create `requirements.lock` or just pinned versions in requirements.txt (reproducible). Prefer pinned versions.
- [x] Create `main.py` entry point (runnable as `python main.py`)
- [x] Create `README.md` (doesn’t exist) with setup, env vars, run command, scripts/test description
- [x] Verify `.env` exists and is gitignored (already true). Keep `.env.example` as-is (placeholders only)

## Task 2: Scripts (ground truth)
- [x] Create `scripts/test` executable (or script) that runs the test suite (via chosen framework). Must be the ground truth per TECH.md.
- [x] Check if `scripts/hooks` exists conceptually (check .opencode for hook config). If needed per project conventions, create minimal `scripts/hooks` (or document if none). Avoid assuming; inspect first.
- [x] Make scripts executable as needed. Document behavior in README.

Rationale: scripts/test is required and must be ground truth.

## Task 3: Configuration & .env loading
- [x] Implement config module (`src/config.py`) to load TELEGRAM_BOT_TOKEN and GEMINI_API_KEY from .env using python-dotenv. Treat missing required vars appropriately (fail loudly with clear logged error if TELEGRAM_BOT_TOKEN missing when needed to run). Never log secrets.
- [x] Validate config shape with Pydantic model (typed boundary for config).
- [x] Add tests for config loading (RED first): missing token, present token, ignore secrets in logs.

## Task 4: Telegram models (typed boundary)
- [x] Define minimal Pydantic models for Telegram API: `Chat`, `Message`, `Update`, and the `getUpdates` response shape. Only include fields needed now (chat_id, text, message, update_id). Make fields optional as appropriate (update may have no message/text/chat).
- [x] Models must allow skipping malformed/unexpected updates safely (validation errors produce skip + log). Never crash on partial payloads.
- [x] Write tests first (RED): valid update with text, update with no message, update with no text, malformed JSON shape, missing chat_id in unexpected forms, channel post/no message — test skip behavior.

## Task 5: HTTP client & getUpdates (long polling)
- [x] Implement minimal raw HTTP client wrapper for Telegram Bot API using chosen lib (httpx/requests). Build URL `https://api.telegram.org/bot{token}/getUpdates`.
- [x] Support long polling params: `timeout` (reasonable, e.g. 30s), `offset` (for deduplication), optional `limit`.
- [x] Implement offset accumulation: track last processed `update_id`, pass `offset = last_update_id + 1` to avoid reprocessing.
- [x] Handle network errors/timeouts: individual failures must not kill loop; log and retry appropriately (with backoff minimal). Fail loudly and log for non-critical; degrade gracefully per TECH ("survive individual update failures without dying").
- [x] Write tests first (RED): offset handling, skip processed updates, malformed response handling, network error resilience behavior (mocked).

## Task 6: Core loop & dispatcher (gateway)
- [x] Implement main polling loop: initialize offset (0), poll getUpdates with long polling, parse into typed models, iterate updates.
- [x] For each update: extract chat_id if present (from message/chat). If update cannot be mapped to a usable message context, skip with structured log (never crash).
- [x] On every applicable message, reply with hardcoded string `"Hi Mate"`. Do this via `sendMessage` endpoint (raw HTTP POST). Same message every time, no branching on content.
- [x] Update offset to `update_id + 1` after processing (or attempting) each update to prevent reprocessing.
- [x] Ensure individual update processing errors don’t terminate the loop — catch, log comprehensively, continue.
- [x] Implement with decorator-based logging for key operations (poll, parse, dispatch, send). No bare except:pass, no swallowed exceptions.
- [x] Write tests first (RED): processes update and sends reply, updates offset, survives bad update, survives send failure without dying.

## Task 7: Integration wiring in main.py
- [x] Wire config → client → loop in `main.py`. Entry point runs the loop.
- [x] Graceful shutdown on signals (SIGINT/SIGTERM) — stop loop cleanly, log shutdown.
- [x] Never log token or secrets. Structured logs only.
- [x] Runnable as `python main.py`.

## Task 8: Testing & validation
- [x] Follow RED/GREEN TDD throughout: write failing tests, implement minimal code to pass, refactor.
- [x] All tests must pass via `scripts/test`.
- [x] Cover happy path, malformed updates, missing fields, offset behavior, resilience (individual failures don’t crash).
- [x] Follow the `write-tests` skill guidance when writing tests — behaviour-first, happy path + edge cases.

## Task 9: Documentation & hygiene
- [x] Create/update README.md with: setup (pip install -r requirements.txt), env vars (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY), how to run (`python main.py`), what `scripts/test` does.
- [x] Ensure .gitignore already covers .env, __pycache__, etc (it does; `.pytest_cache/` added). No secrets committed.
- [x] Dependencies pinned in requirements.txt for reproducibility.