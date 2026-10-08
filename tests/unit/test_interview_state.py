"""Unit tests for the interview state driver (Phase 3 — RED first).

The driver is the single owner of per-chat interview state: versioned, typed,
in-memory, keyed by ``chat_id`` (int). Unknown chats are fresh IDLE state (no
exception); corrupt or foreign state is a loud error, never silent.
"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.interview_state import (
    InterviewPhase,
    InterviewState,
    InterviewStateError,
    InterviewStateStore,
    UserProfile,
    utc_now_iso,
)


def _state(chat_id: int, **overrides) -> InterviewState:
    fields = dict(
        chat_id=chat_id,
        phase=InterviewPhase.INTERVIEWING,
        question_index=2,
        answers=[("q1", "a1"), ("q2", "a2")],
        profile=None,
        updated_at=utc_now_iso(),
    )
    fields.update(overrides)
    return InterviewState(**fields)


# --- fresh state ---------------------------------------------------------------


def test_unknown_chat_returns_fresh_idle_state():
    store = InterviewStateStore()

    state = store.get(4242)

    assert state.chat_id == 4242
    assert state.phase is InterviewPhase.IDLE
    assert state.question_index == 0
    assert state.answers == []
    assert state.profile is None


def test_fresh_state_is_versioned():
    store = InterviewStateStore()

    assert store.get(1).schema_version == 1


def test_updated_at_is_iso_8601_utc():
    store = InterviewStateStore()

    parsed = datetime.fromisoformat(store.get(7).updated_at)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


# --- round trip & isolation ----------------------------------------------------


def test_save_and_get_round_trip():
    store = InterviewStateStore()
    state = _state(101)

    store.save(101, state)
    fetched = store.get(101)

    assert fetched == state


def test_get_returns_a_copy_so_unsaved_mutation_does_not_leak():
    """The store is the single writer: mutating a returned state must not mutate
    the stored one until it is saved explicitly."""
    store = InterviewStateStore()
    store.save(102, _state(102))

    fetched = store.get(102)
    fetched.answers.append(("q3", "a3"))

    assert store.get(102).answers == [("q1", "a1"), ("q2", "a2")]


def test_per_chat_isolation():
    store = InterviewStateStore()
    store.save(201, _state(201))
    store.save(
        202,
        _state(
            202,
            phase=InterviewPhase.COMPLETE,
            question_index=7,
            answers=[("other-q", "other-a")],
        ),
    )

    assert store.get(201).phase is InterviewPhase.INTERVIEWING
    assert store.get(202).phase is InterviewPhase.COMPLETE
    assert store.get(201).answers != store.get(202).answers
    assert store.get(202).answers == [("other-q", "other-a")]


# --- delete --------------------------------------------------------------------


def test_delete_removes_only_that_chat():
    store = InterviewStateStore()
    store.save(301, _state(301))
    store.save(302, _state(302))

    store.delete(301)

    assert store.get(301).phase is InterviewPhase.IDLE
    assert store.get(302).chat_id == 302


def test_delete_unknown_chat_is_a_noop():
    store = InterviewStateStore()

    store.delete(999)  # must not raise

    assert store.get(999).phase is InterviewPhase.IDLE


# --- fail loud -----------------------------------------------------------------


def test_get_rejects_non_int_chat_id():
    store = InterviewStateStore()

    with pytest.raises(InterviewStateError):
        store.get("123")  # string-vs-int drift is a named category


def test_save_rejects_non_int_chat_id():
    store = InterviewStateStore()

    with pytest.raises(InterviewStateError):
        store.save("123", _state(123))


def test_save_rejects_state_with_mismatched_chat_id():
    store = InterviewStateStore()

    with pytest.raises(InterviewStateError):
        store.save(501, _state(502))


def test_save_rejects_non_state_objects():
    store = InterviewStateStore()

    with pytest.raises(InterviewStateError):
        store.save(503, {"chat_id": 503})  # type: ignore[arg-type]


def test_get_raises_loudly_on_corrupt_stored_state():
    store = InterviewStateStore()
    # Simulate programmer-error/foreign corruption of the shared driver's map.
    store._states[601] = "not-a-state"  # type: ignore[assignment]

    with pytest.raises(InterviewStateError):
        store.get(601)


def test_get_rejects_unsupported_schema_version():
    store = InterviewStateStore()
    # Schema versioning exists so future/foreign states fail predictably.
    corrupt = store.get(602)
    store._states[602] = corrupt.model_copy(update={"schema_version": 2})  # type: ignore[assignment]

    with pytest.raises(InterviewStateError):
        store.get(602)


# --- typed model boundary ------------------------------------------------------


def test_interview_state_rejects_non_int_chat_id():
    with pytest.raises(ValidationError):
        InterviewState(
            chat_id="123",  # type: ignore[arg-type]
            phase=InterviewPhase.IDLE,
            updated_at=utc_now_iso(),
        )


def test_interview_state_rejects_unknown_phase():
    with pytest.raises(ValidationError):
        InterviewState(
            chat_id=1,
            phase="sleeping",  # type: ignore[arg-type]
            updated_at=utc_now_iso(),
        )


def test_user_profile_is_typed():
    profile = UserProfile(chat_id=9, summary="Habits: coffee", suggested_animal="the Night Owl")

    assert profile.chat_id == 9
    assert profile.suggested_animal == "the Night Owl"
