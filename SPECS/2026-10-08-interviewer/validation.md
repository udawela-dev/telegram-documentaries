# Phase 3 — The Interviewer: validation

> **Verified 2026-10-08** against the committed suite via `bash scripts/hooks`
> (`191 passed, 2 skipped`). Everything below is verified by the offline
> unit/component suites **except** the live-Telegram checks, which require the
> user's Telegram client. The 2 skips are the pre-existing live-Gemini Bouncer
> tests (`tests/integration/test_live_bouncer.py`); the Gemini key is still
> API-blocked (403), so no live-Gemini claim is made for this feature.

## Acceptance criteria
- [x] `scripts/hooks` green (all offline suites incl. the new interviewer suites;
      the 2 live-Gemini skips remain). — `191 passed, 2 skipped`.
- [x] A human photo approval starts the interview: after the two Bouncer
      verdict messages, exactly one question is asked. — component
      `test_approved_photo_sends_two_bouncer_messages_then_first_question`
      asserts exactly `[Hi Mate, Human detected ✓, Q1]`.
- [x] Questions are asked strictly one at a time; each answer is stored and the
      next question only arrives on the next message. — unit
      `test_each_answer_returns_exactly_one_question`,
      `test_answers_advance_and_store_pairs_in_order`; component
      `test_answers_advance_one_question_per_message_and_complete`.
- [x] Chat isolation: two chats at different points in the interview never see
      each other's state (component-tested; spot-check in Telegram with two
      chats or reset in between). — unit `test_two_chats_never_share_state`,
      `test_per_chat_isolation`; component
      `test_two_chats_interleaved_never_cross_state`,
      `test_rejection_resets_only_the_rejected_chats_interviewer_state`.
      Live spot-check still **pending user**.
- [x] Interview resumes correctly: interrupting after Q3 and sending the next
      text continues at Q4. — unit
      `test_interrupted_interview_resumes_with_the_right_question`.
- [x] After the 7th answer: a profile covering habits, quirks, routines and
      preferences is produced with a clearly identified suggested animal, and
      `event=interview_completed` is logged with the animal. — unit
      `test_profile_covers_all_four_labels_and_suggested_animal`; component
      `test_completion_reply_contains_profile_labels_and_suggested_animal`,
      `test_interview_events_are_logged`.
- [x] The profile + suggested animal are handed to the next stage (stored on
      the shared state driver, contract `UserProfile`). — unit
      `test_completed_profile_is_stored_on_the_shared_driver`; typed contract in
      `src/interview_state.py::UserProfile`.
- [x] `/start` and `/restart` reset both Bouncer session and Interviewer state.
      — component `test_restart_mid_interview_resets_both_stages_and_confirms`,
      `test_start_command_also_resets`.
- [x] Rejected photo: Bouncer rejection flow unchanged and Interviewer state
      purged (next text is "Hi Mate"). — component
      `test_rejected_photo_keeps_rejection_flow_and_purges_interview_state`.
- [x] Bouncer / confirmation flows unchanged for `IDLE` chats. — component
      `test_idle_text_still_gets_hi_mate`,
      `test_gateway_without_interviewer_keeps_phase1_text_behaviour`; full
      `test_gateway_bouncer.py` suite (14 passed).

## Live check (bot in Telegram, key-free local path) — **pending user's Telegram**
- [ ] (needs user's Telegram) Person photo → "Hi Mate" + "Human detected ✓" + Q1.
- [ ] (needs user's Telegram) Answer 7 questions one per message → sequential
      progression.
- [ ] (needs user's Telegram) Interrupt mid-interview (send a text after idle
      time), then resume → correct next question.
- [ ] (needs user's Telegram) Final reply contains Habits / Quirks / Routines /
      Preferences and "Suggested animal: …".
- [ ] (needs user's Telegram) `/restart` → "Hi Mate" flow returns after a fresh
      photo.
- [ ] (needs user's Telegram) Non-human photo mid-interview → rejection + reset.

Component-level equivalents for every live item above pass offline (see the
acceptance criteria).

## Docs
- [x] README + this file updated to match implemented behaviour.
- [x] (User-reviewed) TECH.md / ROADMAP.md Phase 3 lines reflect reality —
      updated this verification pass; pending the user's review.
