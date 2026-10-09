# Phase 5 — The Scripter: validation

> Authoring date: 2026-10-09 on `feature/2026-10-09-scripter` (cut from
> `feature/2026-10-08-converter` tip `62c9d46`). Verified 2026-10-09 on the
> same branch. Ground truth: `bash scripts/hooks` → **342 passed, 4 skipped**
> (the skips are the guarded live-Gemini tests: Bouncer ×2, Converter ×1,
> Scripter ×1). Evidence lines name the covering tests/file; "live" items stay
> unticked until confirmed in Telegram.

## Acceptance criteria
- [x] `scripts/hooks` green (all offline suites incl. the new scripter suites;
      the guarded live tests stay skipped without a healthy key).
      Evidence: `342 passed, 4 skipped`; `python3 -m pytest tests/integration -q`
      → `2 passed, 4 skipped` (the 4 live-Gemini tests skip without
      `RUN_LIVE_GEMINI=1`; `test_main_flow` passes).
- [x] One typed hand-off feeds the Scripter: the completed `UserProfile`
      (`summary` + `suggested_animal`) is read from the shared
      `InterviewStateStore`, keyed by `chat_id` (int).
      Evidence: the Gateway passes the typed `UserProfile` into
      `Scripter.write_script(chat_id, profile)` (`src/gateway.py`
      `_handle_scripting`, reachable only after a successful conversion with a
      profile); `test_write_script_rejects_a_non_profile`,
      `test_write_script_rejects_a_non_int_chat_id`.
- [x] The Scripter is an ADK `LlmAgent` on `gemini-3.1-flash-lite`
      (`SCRIPTER_MODEL` env-overridable) whose prompt embeds the profile summary
      and suggested animal and requests one 60–90 word paragraph, no markdown.
      Evidence: `test_default_model_is_gemini_flash_lite`,
      `test_prompt_text_embeds_profile_summary_animal_and_shape_requests`,
      `test_prompt_content_is_exactly_one_text_part`,
      `test_resolve_model_honours_env_override`,
      `test_resolve_model_uses_default_when_env_absent`.
- [x] The deterministic local validator (`validate_script`: one paragraph,
      60–90 words, markdown sniff) is the single shape gate for **both**
      generation paths; a shape-violating output is never stored or handed to
      TTS, and it triggers the local fallback.
      Evidence: `test_validate_script_accepts_the_boundary_word_counts` (60/90
      ok), `test_validate_script_rejects_out_of_budget_word_counts` (59/91 no),
      `test_validate_script_rejects_two_paragraph_blocks`,
      `test_validate_script_rejects_markdown`,
      `test_validate_script_collapses_whitespace_within_one_paragraph`;
      `test_shape_rejected_llm_output_falls_back_and_never_stores_off_spec_text`
      (`event=script_validation_failed`, no off-spec text stored).
- [x] Key-free resilience: LLM failure / timeout / blocked key / rejected shape
      → deterministic `LocalScriptWriter` output (60–90 words, animal-grounded);
      neither available → loud `ScripterError`, graceful
      `SCRIPTER_REPLY_UNAVAILABLE`, state untouched, loop survives.
      Evidence: `test_llm_failure_uses_local_writer_and_stores_its_script`,
      `test_timeout_falls_back_to_the_local_writer_within_budget`,
      `test_no_gemini_key_goes_straight_to_the_local_writer`,
      `test_no_local_writer_and_failing_llm_raises_and_leaves_state_untouched`,
      `test_off_spec_local_writer_raises_loudly_and_leaves_state_untouched`;
      `tests/unit/test_local_script.py` (deterministic + valid + animal-grounded
      for the default and every canonical animal); gateway
      `test_scripter_failure_sends_unavailable_and_leaves_state_untouched`.
- [x] The raw script string is stored on the shared `InterviewState`
      (`script=...`, `schema_version=2`) via the driver; phase/profile/answers
      are not altered; a v1 state migrates losslessly; a newer-than-current
      schema fails loud.
      Evidence: `test_write_script_stores_the_scripted_llm_text_on_the_shared_state`,
      `test_stored_script_record_carries_the_current_schema_version`,
      `test_stored_script_round_trips_on_the_shared_driver`;
      `tests/unit/test_interview_state.py` migration suite
      (`test_get_migrates_a_v1_record_to_v2_losslessly`,
      `test_save_migrates_a_v1_record_to_v2_losslessly` with
      `event=state_migrated`; `test_get_rejects_unsupported_schema_version`,
      `test_save_rejects_newer_than_current_schema_version` → loud
      `InterviewStateError`).
- [x] Runs automatically after the profile text **and** the hybrid image have
      been sent for that chat; the script text is delivered to the chat as a
      message (**order: profile text → hybrid photo → script text**) and the raw
      string is stored in state for Phase 6. `scripter=None` keeps Phases 3+4
      behaviour unchanged (regression).
      Evidence: component `test_full_pipeline_sends_profile_then_photo_then_script_and_stores_it`
      (asserts the `GateClient.sent` order message(profile) → photo →
      message(script) and `state.script == script`),
      `test_the_stored_script_is_the_same_text_sent_to_the_chat`,
      `test_scripter_none_keeps_phases_3_4_behaviour`,
      `test_converter_failure_does_not_invoke_the_scripter`,
      `test_converter_none_does_not_invoke_the_scripter`,
      `test_send_photo_failure_does_not_invoke_the_scripter`.
- [x] `SCRIPTER_REPLY_UNAVAILABLE` copy-locked; `SCRIPTER_MODEL` /
      `SCRIPTER_GEMINI_TIMEOUT` env-overridable.
      Evidence: `test_unavailable_reply_copy_is_locked`,
      `test_word_budget_constants_are_locked`,
      `test_default_gemini_timeout_is_locked` (`60.0`),
      `test_resolve_model_honours_env_override`.

## Live check (bot in Telegram) — needs user
- [ ] (needs user's Telegram) Person photo → approved → 7 answers → profile
      text, then the hybrid photo, then **the script text message arrives in
      the same chat**, the bot logs `event=script_stored` (today:
      `source=local`) with no `SCRIPTER_REPLY_UNAVAILABLE`, and the raw script
      is stored in state for Phase 6. The real `gemini-3.1-flash-lite`
      paragraph is exercised by the guarded live test the moment a healthy key
      exists.
- [ ] (needs user's Telegram) `/restart` after a completed run clears the stored
      script; a fresh cycle still works.
- [ ] (needs user's Telegram) Non-human photo mid-interview → rejection +
      state purge (incl. script).

## Docs
- [x] README + validation.md updated to match implemented behaviour; TECH.md /
      ROADMAP.md Phase 5 lines reflect reality (incl. the key-blocked note).
      Evidence: README current state "Phases 1-5", Scripter bullet + behaviour
      table (profile → photo → script) + `SCRIPTER_MODEL` / `SCRIPTER_GEMINI_TIMEOUT`
      tuning + architecture line + live-test command incl. `test_live_scripter.py`;
      TECH.md Scripter contract + session state (script field, schema v2,
      migration) + key-free resilience; ROADMAP.md Phase 5 marked Implemented
      with the key-blocked note; requirements.md "store then send" prose aligned
      with the code; `src/gateway.py` docstring gained the Phase 5 bullet.