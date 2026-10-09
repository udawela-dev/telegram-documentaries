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
   - **B2 (2026-10-09, live-verified)** — the real TTS model returns **headerless LINEAR16 PCM** (`audio/L16;codec=pcm;rate=24000`, mono, s16le); staging it as an opaque `.tmp` made ffmpeg exit 1 ("Invalid data found when processing input") and the voice note never arrived. The seam now ends mime-driven format detection (`_source_format` / `_is_linear_pcm_mime` / `_pcm_sample_rate` in `src/narrator.py`): self-describing WAV → `.wav` probed by ffmpeg; declared LINEAR16 → `.pcm` + explicit `-f s16le -ar <rate> -ac 1`; anything else → opaque `.tmp` probed by content (fails loudly rather than force-decoding as PCM). N1 review hardening: the declaration is keyed on the response mime, never on "not a WAV". Evidence: real-ffmpeg tests (`test_real_ffmpeg_converts_a_wav_header_payload_to_oggs`, `test_real_ffmpeg_converts_headerless_pcm_payload_to_oggs`, negative control `test_real_ffmpeg_probing_an_opaque_tmp_headerless_pcm_source_fails` — all `skipif(shutil.which("ffmpeg") is None)`) and **live**: `RUN_LIVE_GEMINI=1` narrator test passed on a healthy key (2026-10-09, feature/2026-10-09-offline-verdict-labels) — real TTS audio converted to Telegram-compatible `OggS` bytes.
8. **Regression**: `narrator=None` keeps Phases 1–5 unchanged (no voice note).
   - [x] Evidence: `tests/component/test_gateway_narrator.py::test_narrator_none_keeps_phases_up_to_5` (GateClient messages end at the script text).
9. **Send failure handling**: `send_voice` failure logged as `event=narrator_send_failed`; no `NARRATOR_REPLY_UNAVAILABLE` sent (locked behavior). Script remains stored.
   - [x] Evidence: component send-failure path asserts `narrator_send_failed` logged, no unavailable reply, state.script still present, loop survives.
10. **Redaction/logging**: structured logging, no secrets/tokens logged. Temp paths not leaking sensitive info.
    - [x] Evidence: redaction unit tests unchanged/green; secret scan (AIza… / bot-token patterns) across the diff and new files: no matches.

## Tests

- Unit tests: `tests/unit/test_narrator.py` (narrator+main tests incl. seams, B1/B2 locks, real-ffmpeg conversion tests, timeout, conversion, errors, cleanup, copy-locks, ADK-absence).
- Component tests: `tests/component/test_gateway_narrator.py` — GateClient `sent_voices`, four-message ordering, regression, conversion/send-failure paths; `tests/unit/test_main.py` key-present/keyless readiness logging.
- Guarded live: `tests/integration/test_live_narrator.py` with `RUN_LIVE_GEMINI=1` + healthy key — asserts real audio bytes and OGG/Opus magic; **PASSED 2026-10-09** on `feature/2026-10-09-offline-verdict-labels` with a healthy key (OggS magic verified; converter live test remains blocked on Google 429 image-quota — environmental).
- All offline tests pass: `bash scripts/hooks` → **422 passed, 5 skipped** (the 5 guarded live-Gemini skips: Bouncer ×2, Converter ×1, Scripter ×1, Narrator ×1).

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

## Live (partially closed 2026-10-09 — healthy key was available)

- [x] `RUN_LIVE_GEMINI=1` narrator live test: real TTS audio bytes, converted to
      Telegram-compatible OGG/Opus (`OggS` magic) — passed on
      `feature/2026-10-09-offline-verdict-labels` with the healthy key.
- [ ] Real Telegram run: photo → 7 answers → profile text → hybrid photo →
      script text → **playable voice note** in chat 8815679590. (2026-10-09
      03:54 log shows the real pipeline reached `event=narrator_started` with a
      Gemini-written script, then `narrator_failed stage=synthesize` under the
      pre-fix seam; needs a restart on the fixed code + healthy key to deliver
      the voice note.)
- [ ] Merge of PRs #1 → #5 → #6 → #7 (user merges; never by the build agent).

## Merge Readiness

Implementation complete, review-approved (B1 fixed: `source` is code-assigned,
never model-parsed), offline suite green (422 passed / 5 skipped), docs synced,
live narrator + scripter + bouncer tests green on a healthy key. PR #7 open on
`feature/2026-10-09-offline-verdict-labels`. The voice note only needs a
restart of the bot with a healthy `GEMINI_API_KEY` (the live bot still runs the
pre-fix seam from its 03:52 boot).