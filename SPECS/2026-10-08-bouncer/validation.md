# Validation: The Bouncer — ADK vision gate (Phase 2)

## Acceptance criteria
- [x] `scripts/hooks` green (offline suite — 99 passed at implementation time).
- [x] Person photo accepted → confirmation flow continues (`"Hi Mate"`).
      (Verified in code path + offline gate tests; live verdict below blocked.)
- [x] Non-human photo rejected → cheeky rejection; chat ephemeral state reset.
      (Verified in code path + offline gate tests; live verdict below blocked.)
- [x] Uncertain/parse-failure → reject safely (unit tests).
- [x] Download/classify failure → graceful reply, loop survives, no reset
      (component tests; also observed live when Gemini refused the key).
- [x] Text messages keep Phase 1 behaviour unchanged (regression tests + live).
- [x] Chat isolation: resetting one chat never clears another (unit tests).
- [~] Live tests (`RUN_LIVE_GEMINI=1`): **BLOCKED** — see Live verdict record.

## Technical validation
- [x] ADK `LlmAgent` on Gemini 3.1 Flash Lite; in-process `Runner` +
      `InMemorySessionService`; no hardcoded keys; `BOUNCER_MODEL` override.
- [x] Typed boundary: `PhotoSize`/`File` models; `BouncerDecision` from strict
      JSON; never raw dicts.
- [x] File download via getFile + file path; errors → `TelegramAPIError`
      (redacted; token never logged). httpx/httpcore silence enforced at the
      client so the token-bearing URLs never reach a log (fixes a latent
      Phase 1 DEBUG-capture leak, hardening tests to match).
- [x] No bare except:pass; failures logged loudly; loop survives.
- [x] `scripts/test` + `scripts/hooks` remain ground truth; README updated;
      `.gitignore` exception added for `tests/fixtures/`.

## Live verdict record (as of 2026-10-08)
⚠️ **Outcome: BLOCKED by Google key flag — verdicts NOT obtainable today.**

- Every `GEMINI_API_KEY` available (original + freshly generated one) returns
  `403 PERMISSION_DENIED "Your API key was reported as leaked. Please use
  another API key."` — verified with a raw HTTPS call to
  `generativelanguage.googleapis.com` (no bot code involved). Google
  permanently disables leaked keys and typically flags the whole project.
- The implementation itself was proven working end-to-end **up to the Gemini
  auth boundary**: the bot downloaded a real photo the user sent, handed the
  bytes to the ADK agent, and Gemini responded (first with a 400 on an input
  shape — fixed — then 403 on the key). The pipeline, models, JSON boundary,
  and gate routing are all exercised by 99 offline tests.

| Live item | Status | Command once a healthy key exists |
| --------- | ------ | --------------------------------- |
| Negative photo (`tests/fixtures/non_human.jpg`) | pending | `RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py::test_non_human_photo_is_rejected -v` |
| Positive photo (`tests/fixtures/person.jpg`) | pending | `RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py::test_person_photo_is_approved -v` |
| Model string actually used | `gemini-3.1-flash-lite` (accepted as a model name by the API — got past name validation; auth failed before generation) | `BOUNCER_MODEL` env override available |

**To finish:** create a new Gemini API key in a **different / never-flagged
project** at https://aistudio.google.com/apikey, write it into `.env`, run the
two commands above, then update the `pending` rows.

## Scope guard
- [x] Constitution files (MISSION/TECH/ROADMAP) unchanged.
- [x] No production changes outside this spec; no secrets committed.