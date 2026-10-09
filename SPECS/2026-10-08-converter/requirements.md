# Phase 4 — The Converter (hybrid portrait)

## Context

Pipeline (ROADMAP): Telegram → Bouncer (Phase 2) → Interviewer (Phase 3) →
**Converter (this feature)** → Scripter → TTS.

Phase 3 keeps the typed hand-off for this stage on the shared drive
(`UserProfile`: summary + `suggested_animal`). Phase 2's gateway downloads the
user's **original portrait** into memory but currently discards it after the
verdict — this feature keeps it.

Decisions confirmed with the user (2026-10-08):

1. New feature branch `feature/2026-10-08-converter`, PR #4 against `main`.
2. **Local composite fallback while Gemini is key-blocked (403).** The real
   `gemini-3.1-flash-image` path is fully implemented — one multimodal call
   (raw portrait bytes + profile text in, image out, no intermediate
   text-generation step) — but while the key stays blocked a deterministic
   OpenCV **photo-booth composite** (animal ears/muzzle/whiskers per the
   suggested animal, placed on the detected face) produces real image bytes so
   the pipeline completes in Telegram today. A healthy key later swaps in the
   real hybrid with no code change.
3. **Completion flow:** on the 7th answer the Interviewer's profile text is
   sent as today, then the Converter's hybrid image is sent automatically in
   the same turn.

## Scope

- **PortraitStore** (in-memory, media counterpart of the shared state driver):
  the approved portrait's raw bytes saved per `chat_id` (int) at approval time;
  purged by `/start`, `/restart`, and rejected photos (reset semantics: purge
  state *and* temp media).
- **The Converter agent** (`gemini-3.1-flash-image` ADK `LlmAgent` +
  `InMemorySessionService` session per chat, `CONVERTER_MODEL` env override):
  - one multimodal `Content` call: `[Blob(portrait bytes), Part(text=prompt)]`;
    the prompt embeds the profile summary + suggested animal + art direction;
  - requests `response_modalities=["IMAGE"]` via
    `LlmAgent.generate_content_config`;
  - extracts the generated image (`part.inline_data.data`) from the final
    response; text-only/empty/malformed response → loud `ConverterError`;
  - bounded execution (daemon thread + `threading.Event`), timeout default 60s
    (`CONVERTER_GEMINI_TIMEOUT`), matching the Bouncer pattern;
  - on LLM failure/timeout/blocked key → local composer fallback (if wired);
    with neither → raises (gateway degrades gracefully).
- **LocalHybridComposer** (OpenCV, key-free): locate the largest face via the
  existing YuNet detector (new `LocalVisionClassifier.largest_face_box`), draw
  a deterministic photo-booth animal overlay determined by the suggested
  animal (archetype → ears/muzzle/whiskers/stripes; locked default style),
  encode and return JPEG bytes. Undecodable input → loud error.
- **Gateway:** save portrait on approval; on interview completion fire the
  converter (profile text first, then `send_photo` to the correct `chat_id`);
  purge portrait on rejection/reset; any converter failure → loud log +
  graceful reply, loop survives.
- **TelegramClient.send_photo(chat_id, bytes, ...)**: multipart `sendPhoto`
  through the existing redacting `_request` path.
- Tests: unit + component (offline), plus a guarded live test
  (`RUN_LIVE_GEMINI=1`) proving the real image call when a healthy key exists.

## Out of scope (YAGNI)

- No on-disk media persistence (TECH.md: in-memory only).
- No caption/labels beyond one optional caption on the sent photo.
- No Scripter/TTS integration — this stage only emits the hybrid image.

## Locked decisions (copy-locked by tests)

- `CONVERTER_REPLY_UNAVAILABLE` reply text (locked string) used when the
  converter cannot run.
- `CONVERTER_MODEL` default `gemini-3.1-flash-image`; `resolve_model()`
  re-reads the env at call time (house pattern).
- Single multimodal call: exactly one `Content` with the portrait as the first
  `inline_data` part and the instruction text as the second part; no
  intermediate text-generation step (asserted via a scriptable LLM seam +
  content-assembly unit tests).
- Prompt text must embed `profile.summary` and `profile.suggested_animal`
  verbatim markers (`Suggested animal:`).
- `chat_id` typed `int` everywhere (house rule); corrupt/wrong-typed store
  entries and wrong-typed `chat_id` → loud `PortraitStoreError`.
- `LocalVisionClassifier.largest_face_box` returns `(x, y, w, h) | None` in
  original-image coordinates (2× rescue pass scaled back); same YuNet model,
  same threshold.
- Business logic never lives in log statements (event logging from module
  functions/callers); illegal/missing inputs fail loud, gateway degrades.

## Contracts

```python
# src/portrait_store.py
class PortraitStoreError(RuntimeError): ...

class PortraitStore:
    save(chat_id: int, image_bytes: bytes) -> None      # wrong type -> loud
    get(chat_id: int) -> bytes | None                   # None = not saved yet
    delete(chat_id: int) -> None                        # idempotent
    # int-only chat_id; a stored non-bytes value -> PortraitStoreError

# src/converter.py
class Converter:
    hybridize(chat_id: int, portrait: bytes, profile: UserProfile) -> bytes
    # -> JPEG/PNG bytes of the hybrid image (LLM or local composer);
    # raises ConverterError when nothing can produce an image.
    # _run_llm(content, session_id) -> bytes   # scriptable seam (bytes, not text!)

# src/local_composite.py
class LocalHybridComposer:
    compose(portrait_bytes: bytes, animal: str) -> bytes  # JPEG/PNG bytes

# gateway
Gateway(client, *, bouncer=..., interviewer=..., converter=None,
        portraits=None)
# None converter => interview completion behaves exactly as Phase 3.
```

## Reset semantics (unchanged + media)

- `/start` `/restart` and rejected photos purge: Bouncer session, Interviewer
  state, **and** the chat's portrait.
- Missing portrait or profile at completion → loud log + graceful
  `CONVERTER_REPLY_UNAVAILABLE`, loop survives, state untouched.