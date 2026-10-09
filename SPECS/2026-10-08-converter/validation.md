# Phase 4 — The Converter: validation

> Verified 2026-10-09 on `feature/2026-10-08-converter`. Ground truth:
> `bash scripts/hooks` → **272 passed, 3 skipped** (the 3 skips are the guarded
> live-Gemini tests: Bouncer ×2 + Converter ×1). Evidence lines name the
> covering tests/file; "live" items stay unticked until the user confirms in
> Telegram.

## Acceptance criteria
- [x] `scripts/hooks` green (all offline suites incl. the new converter suites;
      the live-Gemini skips remain skipped without a key).
      Evidence: `272 passed, 3 skipped`; `python3 -m pytest tests/integration -q`
      → `2 passed, 3 skipped` (test_main_flow passes; 3 live tests skip).
- [x] The approved portrait's raw bytes are retained per `chat_id`
      (`PortraitStore`) and purged by `/start`, `/restart`, and rejected photos.
      Evidence: `tests/unit/test_portrait_store.py` (25 tests); component
      `test_approved_photo_saves_the_downloaded_portrait_for_that_chat`,
      `test_restart_purges_the_stored_portrait_and_converter_session`,
      `test_rejected_photo_purges_the_stored_portrait_and_converter_session`,
      `test_reset_purges_only_the_reset_chats_portrait`; gateway `_handle_photo`
      save + `_purge_portrait` on reset/rejection.
- [x] On the 7th answer the Interviewer's profile text is sent, then the
      Converter fires automatically: exactly one multimodal call (raw image +
      profile text in one `Content`, `response_modalities=["IMAGE"]`, no
      intermediate text-generation step — unit-proven via the content seam).
      Evidence: `test_completion_sends_profile_text_then_exactly_one_photo_with_hybrid_bytes`
      asserts `calls[-2] == message(profile.summary)` then `calls[-1] == photo`;
      `test_prompt_content_is_exactly_portrait_blob_then_instruction_text`,
      `test_agent_requests_image_modality_via_generate_content_config`,
      `test_hybridize_returns_scripted_llm_image_in_a_single_call` (`llm_calls ==
      [(2, "4242")]`).
- [x] The generated hybrid image is returned to the correct `chat_id` via
      `send_photo` (`event=hybrid_sent`).
      Evidence: `test_completion_sends_profile_text_then_exactly_one_photo_with_hybrid_bytes`
      (`sent_photos == [(222, HYBRID_BYTES)]`);
      `test_conversion_events_are_logged` asserts `event=hybrid_sent`; gateway
      `_handle_conversion` sends photo to `chat_id`.
- [x] LLM unreachable/blocked key → local composite fallback produces real
      image bytes. Neither available → loud `ConverterError`, graceful reply,
      loop survives.
      Evidence: `test_llm_failure_uses_local_composer_with_portrait_and_animal`
      (`event=converter_local_fallback`), `test_timeout_falls_back_to_local_composer`
      (bounded), `test_hybridize_with_llm_failure_and_no_composer_raises`,
      `test_converter_failure_degrades_gracefully_and_loop_survives`; local
      composer tests decode real JPEG bytes via OpenCV. The in-Telegram
      demonstration of the fallback is tracked in the live section below.
- [x] Missing portrait / missing profile at completion → loud log + graceful
      reply; state untouched.
      Evidence: `test_completion_with_no_saved_portrait_is_unavailable_and_logged`
      (`event=converter_portrait_missing`),
      `test_complete_without_profile_is_unavailable_and_logged`
      (`event=converter_profile_missing`); gateway graceful
      `CONVERTER_REPLY_UNAVAILABLE`, no state mutation.
- [x] Bouncer + Interviewer workflows, session state and architecture
      unchanged (`converter=None` regression suite).
      Evidence: `test_converter_none_keeps_phase3_completion_behaviour`,
      `test_portraits_none_keeps_phase3_completion_behaviour`, and Phases 1–3
      suites all green; `converter=None`/`portraits=None` produce no extra
      replies or converter events and the Phase-3 profile re-send is intact.
- [x] `CONVERTER_REPLY_UNAVAILABLE` copy-locked; `CONVERTER_MODEL`
      env-overridable.
      Evidence: `test_unavailable_reply_copy_is_locked`,
      `test_resolve_model_honours_env_override`, `test_default_model_is_gemini_flash_image`,
      `test_constructor_honours_explicit_model`.

## Live check (bot in Telegram) — needs user
- [ ] (needs user's Telegram) Person photo → approved → 7 sequential answers →
      profile text arrives, then a hybrid image arrives in the same chat.
      **Today:** the local photo-booth composite (Gemini key still 403).
      The real `gemini-3.1-flash-image` hybrid is exercised by the guarded
      live test the moment a healthy key exists.
- [ ] (needs user's Telegram) `/restart` then a photo → fresh interview works.
- [ ] (needs user's Telegram) Non-human photo mid-interview → rejection +
      state/portrait purge.

## Docs
- [x] README + this file updated to match implemented behaviour; TECH.md /
      ROADMAP.md Phase 4 lines reflect reality (incl. the key-blocked note).
      Evidence: README current-state/behaviour + Converter tuning updated;
      TECH.md architecture/session-state Converter notes; ROADMAP.md Phase 4
      marked Implemented with the key-blocked note.
