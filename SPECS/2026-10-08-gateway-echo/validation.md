# Validation: Repository & gateway (Phase 1)

Status: **VERIFIED PASS** on branch `feature/2026-10-08-gateway-echo`
(52 tests green via `scripts/hooks`; live smoke test passed against the real
Telegram API on 2026-10-08).

## Acceptance criteria (must all pass)
- [x] `scripts/test` passes with no failures (52 passed)
- [x] Bot receives an update over long polling and replies with hardcoded "Hi Mate"
      (live smoke test: `event=reply_sent ... text='Hi Mate'` ×7 to chat 8815679590)
- [x] No webhook, no public URL required
- [x] Entry point runnable as `python main.py`

## Technical validation
- [x] Raw HTTP to Telegram `getUpdates` (no python-telegram-bot, no google-adk telegram integration)
- [x] Long polling uses offset/offset accumulation; same update never processed twice
- [x] Individual update failures do not kill the loop
- [x] Typed Pydantic models at boundary; raw dicts never passed across module boundaries
- [x] Malformed/unexpected updates skipped with log, never crash loop
- [x] Secrets loaded from .env; never printed/logged/committed (redaction filter + tests; smoke log grep = 0 hits)
- [x] Comprehensive structured logging via decorators where sensible (`log_call`, `event=` records)
- [x] No bare except:pass, no swallowed exceptions, no un-logged fallbacks
- [x] Dependencies pinned in requirements.txt; installable via pip
- [x] scripts/test is ground truth and defined (also `scripts/hooks`)
- [x] README.md documents setup, env vars, how to run, what scripts/test does
- [x] Same hardcoded message on every applicable message; no LLM/Gemini calls

## Test coverage (TDD)
- [x] Config loading tests (present/missing token, no secret leakage)
- [x] Telegram model validation tests (valid, missing message/text/chat, malformed)
- [x] Offset handling tests (getUpdates with offset increments)
- [x] Dispatch/reply tests (sends "Hi Mate" on message)
- [x] Resilience tests (bad update doesn’t crash loop; send failure handled without dying)
- [x] Integration tests cover main flow (against a local HTTP stub — real sockets, no live API)

## Verification steps
1. [x] Run `bash scripts/test` from repo root — all tests pass
2. [x] Manual smoke test: set TELEGRAM_BOT_TOKEN, run `python main.py`, send message to bot from phone — bot replies "Hi Mate" each time
3. [x] Inspect logs: no token appears in logs; malformed updates produce structured log entries
4. [x] Confirm no webhook config, no Gemini usage

## Merge readiness
- [x] All validation items checked
- [x] Constitution files (MISSION/TECH/ROADMAP) unchanged
- [x] No production code changes outside what spec requires
- [x] README accurate and complete for Phase 1