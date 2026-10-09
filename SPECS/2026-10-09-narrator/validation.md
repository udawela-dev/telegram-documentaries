# Phase 6 — Narrator Validation

> Verification date: 2026-10-09 on `feature/2026-10-09-narrator` (cut from
> `feature/2026-10-09-scripter` @ `08b8f4d`). Offline items are ticked with the
> covering test/file as evidence; "live" items stay unticked until a healthy
> `GEMINI_API_KEY` exists (403-blocked today) or a real Telegram run confirms.

## Success Criteria

1. **Order**: completion delivery order is profile text → hybrid photo → script text → voice note (asserted in component suite).
   - [x] Evidence: `tests/component/test_gateway_narrator.py::test_full_pipeline_sends_profile_photo_script_then_voice` — GateClient `sent` asserts `[message(profile), photo(HYBRID), message(SCRIPT), voice(AUDIO)]`.
2. **Narrator not an agent**: implementation uses direct `client.models.generate_content` (no ADK LlmAgent); narrator.py has no LlmAgent/Runner session management like scripter/converter.
   - [x] Evidence: `tests/unit/test_narrator.py::test_narrator_module_has_no_adk_agent_machinery` (AST source lock: no `adk` imports, no `LlmAgent`/`Runner`); manual read of `src/narrator.py` shows the direct generate_content call with `speech_config`.
3. **No local TTS fallback**: on any TTS failure (blocked key, API error, timeout, malformed response, missing ffmpeg when conversion needed) gateway logs loudly and sends `NARRATOR_REPLY_UNAVAILABLE`; no synthetic/non-natural audio produced.
   - [x] Evidence: `test_narrator_unavailable_copy_locked`, timeout/API-error/ffmpeg-missing unit tests raise `NarratorError`; `tests/component/test_gateway_narrator.py` synth-failure + conversion-failure paths assert the locked copy is sent and the loop survives.
4. **sendVoice delivery**: audio sent via Telegram `sendVoice` (voice note). `telegram_client.send_voice` implemented with multipart `voice` field; response validated.
   - [x] Evidence: `tests/unit/test_telegram_client.py` send_voice tests (multipart `voice` field, validation, redacted `TelegramAPIError`).
5. **Script source**: narration uses `state.script` from shared driver (Phase 5). Missing script → unavailable.
   - [x] Evidence: `tests/component/test_gateway_narrator.py` missing-script path → `event=narrator_failed reason=missing_script` + `NARRATOR_REPLY_UNAVAILABLE`; no TTS attempted. `InterviewState` untouched (schema v2, no new fields).
6. **Temp lifecycle**: audio written to temp file(s), sent, and temp files cleaned up in `finally` (success AND error).
   - [x] Evidence: unit tests assert temp paths unlinked on success and on error/exception.
7. **Conversion seam**: non-sendVoice-compatible format converted to OGG/Opus via ffmpeg if available; missing ffmpeg when needed → graceful unavailable, nothing silently skipped.
   - [x] Evidence: `test_convert_to_ogg_*_forces_ogg_output` (ffmpeg command carries `-f ogg` before an `.ogg` target — B1 lock) and `test_synthesize_converts_non_ogg_audio_end_to_end_to_a_real_ogg_temp_path` (FakeClient WAV payload through the real conversion seam to a real temp path); ffmpeg-missing → `NarratorError`; empty output / vanishing executable / conversion timeout covered.
8. **Regression**: `narrator=None` keeps Phases 1–5 unchanged (no voice note).
   - [x] Evidence: `tests/component/test_gateway_narrator.py::test_narrator_none_keeps_phases_up_to_5` (GateClient messages end at the script text).
9. **Send failure handling**: `send_voice` failure logged as `event=narrator_send_failed`; no `NARRATOR_REPLY_UNAVAILABLE` sent (locked behavior). Script remains stored.
   - [x] Evidence: component send-failure path asserts `narrator_send_failed` logged, no unavailable reply, state.script still present, loop survives.
10. **Redaction/logging**: structured logging, no secrets/tokens logged. Temp paths not leaking sensitive info.
    - [x] Evidence: redaction unit tests unchanged/green; secret scan (AIza… / bot-token patterns) across the diff and new files: no matches.

## Tests

- Unit tests: `tests/unit/test_narrator.py` (47 narrator+main tests incl. seams, B1 locks, timeout, conversion, errors, cleanup, copy-locks, ADK-absence).
- Component tests: `tests/component/test_gateway_narrator.py` — GateClient `sent_voices`, four-message ordering, regression, conversion/send-failure paths; `tests/unit/test_main.py` key-present/keyless readiness logging.
- Guarded live: `tests/integration/test_live_narrator.py` with `RUN_LIVE_GEMINI=1` + healthy key — asserts real audio bytes and OGG/Opus magic; **skipped** today (key 403-blocked).
- All offline tests pass: `bash scripts/hooks` → **403 passed, 5 skipped** (the 5 guarded live-Gemini skips: Bouncer ×2, Converter ×1, Scripter ×1, Narrator ×1).

## Env/Config

- `NARRATOR_MODEL` (default `gemini-3.1-flash-tts-preview`), `NARRATOR_VOICE`
  (default `Orus` — firm/low-register male; overridable), `NARRATOR_GEMINI_TIMEOUT`
  (default 60). Model/voice/timeout resolved at construction (house pattern).

## Evidence / doc sync

- README: current state "Phases 1-6"; Phase 6 bullet + behaviour table (7th
  answer ends with the voice note; order profile → photo → script → voice);
  Narrator env table; "The Narrator stage" note (incl. the deliberate *no fake
  TTS* fallback); live-test command incl. `test_live_narrator.py`; architecture
  line for `src/narrator.py`.
- TECH.md: Narrator contract expanded (direct generate_content +
  speech_config, bounded timeout, temp lifecycle + ffmpeg `-f ogg` seam, no
  local fallback, `narrator_send_failed` semantics, no new state fields) and
  the delivery-order line updated to profile → photo → script → voice.
- ROADMAP.md: Phase 6 marked **Implemented** with the key-blocked note.
- No spec drift: locked copy `NARRATOR_REPLY_UNAVAILABLE`, model default,
  timeout 60, and the event names match `requirements.md` (`plan.md`'s
  `narrator_sent` post-send event name adopted).

## Live (still open — needs a healthy `GEMINI_API_KEY` / user Telegram run)

- [ ] `RUN_LIVE_GEMINI=1` narrator live test: real TTS audio bytes, natural
      deep posh-British delivery, OGG/Opus magic.
- [ ] Real Telegram run: photo → 7 answers → profile text → hybrid photo →
      script text → **playable voice note** in chat 8815679590.
- [ ] Merge of PRs #1 → #5 → #6 (user merges; never by the build agent).

## Merge Readiness

Implementation complete, review-approved, offline suite green (403 passed /
5 skipped), docs synced. Ready to commit on `feature/2026-10-09-narrator` and
open PR #6. Live audio checks remain for a healthy key.