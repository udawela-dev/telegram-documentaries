# Phase 4 — The Converter: plan

TDD (Red/Green) repo. Run checks ONLY via the dev scripts
(`bash scripts/test`, `bash scripts/hooks` — see README).

## Task group 1 — Spec + branch (done)
- Branch `feature/2026-10-08-converter` cut from the Interviewer branch.
- `requirements.md`, `plan.md`, `validation.md` authored.

## Task group 2 — RED tests (unit)
- `tests/unit/test_portrait_store.py`: save/get round-trip preserves exact
  bytes; unknown chat → `None`; two chats isolated; `delete` idempotent and
  chat-scoped; non-int `chat_id` (`str`, `bool`) → `PortraitStoreError`;
  `.save` with non-bytes → loud; corrupted stored value → loud.
- `tests/unit/test_converter.py`:
  - content assembly: `converter._prompt_content(portrait, profile)` returns
    one `Content` whose parts are exactly `[Part(inline_data=Blob(portrait)),
    Part(text=...)]`; text embeds `profile.summary` and
    `profile.suggested_animal`;
  - `_run_llm` seam: scripted bytes flow straight through `hybridize`
    (`event=hybrid_generated`);
  - LLM failure (raises) + local composer wired → composer called with
    `(portrait, animal)` and returns image bytes (`event=converter_local_fallback`);
  - text-only / no-parts / no-inline-data LLM response → loud
    `ConverterError` with no silent success;
  - no composer and failing LLM → raises (never a fake "image");
  - `resolve_model` env override (monkeypatch);
  - copy-lock `CONVERTER_REPLY_UNAVAILABLE`.
- `tests/unit/test_local_composite.py`: decodable portrait → valid JPEG/PNG
  bytes (decodes via cv2.imdecode; non-trivial size); face present vs
  absent portrait both produce bytes (default archetype when no face);
  undecodable bytes → loud; determinism (same input -> same output bytes).
- Extend `tests/unit/test_local_vision.py`: `largest_face_box` returns the
  face box on the person fixture, `None` on the landscape fixture, and
  scaled-back coordinates on a 2×-only detection.

## Task group 3 — Implement
- `src/portrait_store.py` (`PortraitStore`, `PortraitStoreError`).
- `src/local_vision.py`: add `largest_face_box(image_bytes) -> tuple|None`
  (reuse `_detect`; add box-returning internal, upscale-rescue scaled back).
- `src/local_composite.py`: `LocalHybridComposer.compose(portrait, animal)`
  (OpenCV; archetype map incl. locked default; JPEG encode).
- `src/converter.py`: `Converter` (ADK `LlmAgent` on
  `gemini-3.1-flash-image` with `generate_content_config` =
  `GenerateContentConfig(response_modalities=["IMAGE"])`, per-chat sessions,
  `CONVERTER_MODEL` override, `_prompt_content`, `_run_llm` seam, image-part
  extraction, bounded timeout, local-composer fallback,
  `CONVERTER_REPLY_UNAVAILABLE`).
- `src/telegram_client.py`: `send_photo(chat_id, image_bytes, *, filename,
  mime_type)` multipart via the existing redacting `_request` (typed response
  validation, `ok=false` -> `TelegramAPIError`).
- `src/gateway.py`: `portraits` + `converter` constructor params; save portrait
  on approval; completion → profile text then converter → `send_photo`;
  missing portrait/profile → loud + graceful; purge portrait on
  rejection/reset; event logs (`converter_started`, `hybrid_sent`,
  `converter_failed`, `converter_portrait_missing`, `converter_profile_missing`).
- `main.py`: build store + composer + `Converter` (logged
  `event=converter_ready model=... local_fallback=...` / local-only /
  unavailable), wire into `Gateway`.

## Task group 4 — RED tests (component)
- `tests/component/test_gateway_converter.py` (GateClient gains
  `sent_photos: list[tuple[int, bytes]]`):
  - approved photo → `portraits.get(chat) == downloaded bytes`;
  - full interview completion → profile text sent, then exactly one
    `send_photo` with the hybrid bytes to the same chat (`event=hybrid_sent`);
  - converter raises → graceful `CONVERTER_REPLY_UNAVAILABLE`, loop survives,
    next update processed;
  - completion with no saved portrait → unavailable + `converter_portrait_missing`;
  - COMPLETE state without profile → unavailable + `converter_profile_missing`;
  - `/restart` and rejected photo purge the portrait (further completion →
    unavailable);
  - `converter=None` (or `portraits=None`) → Phase 3 behaviour unchanged
    (regression);
  - Photo reply (`send_photo`/`send_message`) failures → logged, loop survives.
- Phase 1–3 regression suites keep passing unchanged.

## Task group 5 — Green
- `bash scripts/test` then `bash scripts/hooks` — all offline suites green;
  the 3 guarded live tests (Bouncer ×2, Converter ×1) stay skipped without
  `RUN_LIVE_GEMINI=1` + a healthy key.

## Task group 6 — Live Telegram check (with user)
- Restart the supervised bot. User sends a person photo, completes the 7
  questions: expect the profile text then an image
  (today: the local photo-booth composite — the real `gemini-3.1-flash-image`
  hybrid appears the moment a healthy key exists). `/restart` + non-human
  photo purge paths worth one spot-check.

## Task group 7 — Docs + PR
- Sync README (behaviour table row: "after 7th answer → profile text + hybrid
  image"; environment knob `CONVERTER_MODEL`, `CONVERTER_GEMINI_TIMEOUT`;
  architecture line) and `SPECS/2026-10-08-converter/validation.md` to what
  was implemented; TIME/ROADMAP Phase 4 marked Implemented (with the
  key-blocked honest note).
- Commit on `feature/2026-10-08-converter`, open PR #4 against `main`, report
  the URL and remaining issues (Gemini key still blocked → real image-gen
  path unexercised live).