# Validation: The Bouncer — ADK vision gate (Phase 2)

## Acceptance criteria
- [x] `scripts/hooks` green (offline suite — 118 passed at implementation time).
- [x] Person photo accepted → confirmation flow continues (`"Hi Mate"`) **and a
      second message announces "Human detected ✓"** (user decision 2026-10-08:
      the verdict is spoken aloud in chat). Verified offline via the key-free
      fallback (person.jpg → human) AND via the Gemini-approval unit path.
- [x] Non-human photo rejected → cheeky rejection, **then "Non-human detected"**,
      then chat ephemeral state reset. Verified offline via the key-free
      fallback (non_human.jpg → non-human) AND via the Gemini-rejection unit
      path.
- [x] Uncertain/parse-failure → reject safely (unit tests).
- [x] Download/classify failure → graceful reply, loop survives, no reset
      (component tests; also observed live when Gemini refused the key).
- [x] Text messages keep Phase 1 behaviour unchanged (regression tests + live).
- [x] Chat isolation: resetting one chat never clears another (unit tests).
- [x] Gemini unreachable/timeout → key-free local fallback verdict (unit tests
      incl. a hard 0.3s-timeout test proving a hung call cannot freeze the bot).
- [~] Gemini live tests (`RUN_LIVE_GEMINI=1`): **BLOCKED** — see Live record.

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

### ✅ Offline human/non-human verdicts — VERIFIED (key-free local fallback)

Because all available Gemini keys are Google-flagged, the offline suite now
verifies the exact behaviour the user asked for — *with the real images, no
key needed* — through the key-free **YuNet** fallback (bundled in
`src/data/`, Apache-2.0). These run in `scripts/hooks` on every commit:

| Test | Fixture | Result |
| ---- | ------- | ------ |
| Negative (object/landscape) | `tests/fixtures/non_human.jpg` (Judean mountains) | **non-human** ✓ |
| Positive (person) | `tests/fixtures/person.jpg` (elderly Gambian woman, face visible) | **human** ✓ |

Both also re-verified through the full `Bouncer.classify` gate (Gemini-down
fallback path, unit tests), plus a hard-timeout test proving a hung Gemini
call cannot freeze the bot (daemon worker + 0.3s synthetic timeout).

### ⚠️ Gemini live verdicts remain BLOCKED by the key flag

- Every `GEMINI_API_KEY` available returns `403 PERMISSION_DENIED "Your API
  key was reported as leaked. Please use another API key."` — verified with a
  raw HTTPS call to `generativelanguage.googleapis.com` (no bot code
  involved). Google permanently disables leaked keys, typically the whole
  project.
- With a healthy key, the bot would classify via Gemini; today it classifies
  via the local detector (same replies, slightly less smart judgment: faces,
  not "face or body").

**To finish:** create a new Gemini API key in a **different / never-flagged
project** at https://aistudio.google.com/apikey, write it into `.env`, run
`RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py -v`,
then update the `pending` rows:

## Scope guard
- [x] Constitution files (MISSION/TECH/ROADMAP) unchanged.
- [x] No production changes outside this spec; no secrets committed.