# Phase 5 — The Scripter (narration)

## Context

Pipeline (ROADMAP): Telegram → Bouncer (2) → Interviewer (3) → Converter (4) →
**Scripter (this feature)** → Narrator/TTS (6).

Phases 3+4 leave the hand-off this stage needs on the shared per-chat driver:
the completed `UserProfile` (`summary` + `suggested_animal`) is stored on the
`InterviewStateStore` when the 7th answer completes, and the gateway then sends
the profile text followed by the Converter's hybrid image. **The Scripter runs
automatically after those two deliveries** for that chat: it produces exactly
one dramatic British-wildlife-documentary paragraph (60–90 words, TTS-ready, no
markdown, no extra commentary), stores the raw script string on the same shared
`InterviewState` driver for the Phase 6 Narrator, and does not replace or alter
any existing pipeline behaviour.

Decisions (recommended house patterns adopted for confirmation — flick any and
the spec is adjusted):

1. **Shape enforcement + key-free resilience (mirrors Interviewer/Converter).**
   A deterministic local validator (`validate_script`: word count 60–90, exactly
   one paragraph, markdown sniff) is the single gate that decides whether an
   output is acceptable. Gemini (an ADK `LlmAgent` on `gemini-3.1-flash-lite`)
   runs first; on failure / timeout / blocked key **or a shape-rejected output**
   the key-free deterministic local writer (`src/local_script.py`) produces a
   valid paragraph instead. With neither available → loud `ScripterError`, and
   the gateway degrades gracefully. The local writer is the **fallback, not a
   toggle default** — it is simply the only live path while `GEMINI_API_KEY` is
   403-blocked, exactly like `local_vision` / `local_composite` before it.
2. **Script lives on the shared InterviewState driver.** A new nullable field
   `script: str | None` is added to `InterviewState`, `schema_version` bumps
   1 → 2, and a **v1 record (in-memory only) is migrated losslessly** to v2
   (`script=None`) with a logged `event=state_migrated`; a record newer than the
   current version fails loud (predictable handling of existing records, TECH.md).
3. **Scripter failure is loud + graceful.** If the scripter cannot produce a
   valid script (None, blocked key, no local writer, store error) the gateway
   logs loudly and replies with the locked `SCRIPTER_REPLY_UNAVAILABLE` copy;
   the polling loop survives and **state is untouched** (`script` stays `None`).
   Mirrors `CONVERTER_REPLY_UNAVAILABLE`.

Also settled (correction confirmed 2026-10-09): the script **IS sent to the
correct Telegram chat as a text message** automatically after the interview +
Converter stages complete. The chat-visible order is exactly **profile text →
hybrid photo → script text**, and the same raw script string is then stored on
the shared state for the Phase 6 Narrator, which renders that same paragraph as
the voice note (TECH.md: the Narrator is not an agent; the script is routed
straight to TTS).

## Scope

- **`src/scripter.py`** — the Scripter agent:
  - ADK `LlmAgent` on `gemini-3.1-flash-lite` (`SCRIPTER_MODEL` env override,
    `resolve_model()` re-reads the env at call time — house pattern), per-call
    fresh-and-reaped `InMemorySessionService` session (delete-then-create +
    `finally` reap, mirroring `Converter`), a `Runner`;
  - one text `Content` turn whose prompt embeds `profile.summary` and
    `profile.suggested_animal`, asking for exactly one 60–90 word
    British-documentary paragraph, no markdown;
  - `_run_llm(content, session_id) -> str` — the scriptable seam for offline
    tests (returns text, the str analogue of the Converter's bytes seam);
  - bounded execution (daemon thread + `threading.Event`),
    `SCRIPTER_GEMINI_TIMEOUT` default 60s (Bouncer/Converter pattern);
  - `validate_script(text) -> str | None` — deterministic local validator at
    the model-output boundary: collapses whitespace, requires exactly one
    paragraph block, 60–90 words (locked `SCRIPTER_MIN_WORDS`/`SCRIPTER_MAX_WORDS`),
    and no markdown markers; valid → normalized text, invalid → `None` (never a
    silent acceptance of a shape-violating script);
  - Gemini-first, local-fallback: on any LLM failure/timeout/blocked key **or a
    rejected shape** → key-free `LocalScriptWriter` (if wired); with neither →
    raises `ScripterError` (never a fake/off-spec "script");
  - `write_script(chat_id: int, profile: UserProfile) -> str` — the public
    entry: validates types, generates + validates, persists the script onto the
    shared driver (read state → `model_copy(update={"script": ..., "schema_version":
    2, "updated_at": now})` → `save`, preserving phase/profile), logs
    `event=script_generated source=gemini|local` then `event=script_stored`.
- **`src/local_script.py`** — `LocalScriptWriter.write(profile: UserProfile) -> str`:
  deterministic, key-free, one 60–90 word paragraph in British-documentary
  voice grounded in `profile.suggested_animal` + a locked narrative template;
  identical input → identical output (tests lock word count across every
  canonical animal + the default).
- **`src/interview_state.py`** — `script: str | None = None` field;
  `schema_version` default → `2`; one `_normalize`/migration helper shared by
  `get` and `save`: migrate v1 → v2 (logged), reject newer-than-current loudly.
- **Gateway** — new optional `scripter=` constructor param. Inside
  `_handle_conversion`, after `event=hybrid_sent`, `_handle_scripting` fires the
  scripter for that chat: `write_script` generates + validates + persists the
  raw script on the shared driver, then, on success, the gateway **sends the
  validated paragraph back to the same chat via `send_message` — chat order:
  profile text → hybrid photo → script text** (storage first, delivery second;
  a delivery failure still leaves the raw script stored for Phase 6, logged
  loud as `event=script_send_failed` with no misleading apology). `scripter=None`
  → Phases 3+4 behaviour exactly unchanged (regression gate, mirroring
  `converter=None`); scripter failure / `None` profile → loud log + locked
  graceful reply, state untouched, loop survives; no scripter when conversion
  itself was skipped or failed.
- **`main.py`** — build the pure-Python `LocalScriptWriter` and the `Scripter`
  on the **shared** `interview_store`, wire into the `Gateway` (logged
  `event=scripter_ready model=... local_fallback=true`).
- Tests: unit + component (offline), plus a guarded live test
  (`RUN_LIVE_GEMINI=1`) proving the real Flash-Lite paragraph when a healthy
  key exists.

## Out of scope (YAGNI)

- **No TTS / voice note** in this phase — that is Phase 6 (Narrator), which will
  read `state.script`.
- **No script regeneration or retries** — the script is produced once per
  completed run and sent as a single text message. The pipeline's profile text
  and hybrid image delivery are unchanged.
- **No on-disk persistence** (TECH.md: in-memory only).
- **No new config toggles** beyond the single env model/timeout knob pair.

## Locked decisions (copy-locked by tests)

- `SCRIPTER_REPLY_UNAVAILABLE` reply text (locked string) used when the scripter
  cannot run: "Hang on — the narrator's quill ran dry. Give that another go?"
- `DEFAULT_SCRIPTER_MODEL = "gemini-3.1-flash-lite"`; `resolve_model()` re-reads
  `SCRIPTER_MODEL` at call time (house pattern).
- `SCRIPTER_MIN_WORDS = 60`, `SCRIPTER_MAX_WORDS = 90`; the output contract is
  exactly one paragraph within that budget, no markdown — enforced by
  `validate_script` for both generation paths.
- `_run_llm` is the scriptable seam; it returns **text**. Shape acceptance of
  any output (LLM or local) goes through `validate_script` — no silent
  off-spec script is ever stored or handed to TTS.
- Prompt must embed `profile.summary` and `profile.suggested_animal` verbatim
  markers (`Suggested animal:`) and request "one paragraph", "60–90 words", "no
  markdown".
- `chat_id` typed `int` everywhere (house rule); wrong-typed `chat_id`, corrupt
  stored state, or a state newer than the current schema → loud
  `InterviewStateError` / `ScripterError` (never a silent fallback).
- The script is persisted only via the shared driver; the Scripter never mutates
  the phase, profile, or answers of a chat.
- The chat-visible completion order is locked: profile text → hybrid photo →
  script text (asserted via `GateClient.sent` in the component suite). The
  gateway sends the script text, and the Scripter stores the raw string.

## Contracts

```python
# src/scripter.py
SCRIPTER_REPLY_UNAVAILABLE: str        # locked copy
SCRIPTER_MIN_WORDS = 60
SCRIPTER_MAX_WORDS = 90

def validate_script(text: str) -> str | None
# -> normalized single-paragraph text when 60-90 words and markdown-free;
#    None when the shape contract is violated (never raise, never assume).

class ScripterError(RuntimeError): ... # nothing could produce a valid script

class Scripter:
    write_script(chat_id: int, profile: UserProfile) -> str
    # -> validated script text (Gemini or local writer); stored on the shared
    #    InterviewState (script=..., schema_version=2); raises ScripterError
    #    when no valid script can be produced or the store rejects the write.
    # _run_llm(content, session_id) -> str   # scriptable seam (text, not bytes)

# src/local_script.py
class LocalScriptWriter:
    write(profile: UserProfile) -> str  # deterministic, 60-90 words, no markdown

# src/interview_state.py (schema_version 1 -> 2; migration of v1 records)
class InterviewState(BaseModel):
    ...
    script: str | None = None
    schema_version: int = 2

# gateway
Gateway(client, *, bouncer=..., interviewer=..., converter=None,
        portraits=None, scripter=None)
# None scripter => interview completion behaves exactly as Phases 3+4.
# On success the gateway sends the script text via send_message to the same
# chat (after the hybrid photo) and the raw script is stored on the driver.
```

## Reset semantics / error handling (unchanged + script)

- `/start`, `/restart`, and rejected photos purge the Interviewer state (which
  `interviewer.reset` already does via the store) — the `script` field lives on
  that state, so a reset clears it too. The Scripter holds no extra per-chat
  state beyond the one-shot session it reaps per call.
- Scripter failure (`ScripterError`, `None`, store error) → loud log
  (`event=script_failed`) + graceful `SCRIPTER_REPLY_UNAVAILABLE`, loop
  survives, `state.script` untouched (stays `None`).
- No scripter fires when conversion was skipped (no converter/portrait wired) or
  failed (no image delivered) — the script is produced only after the profile
  text *and* the hybrid image have been sent for that chat.