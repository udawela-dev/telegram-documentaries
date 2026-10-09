"""Unit tests for The Interviewer (Phase 3 — RED first).

Offline strategy: the curated question bank and the profile/animal builder are
deterministic and local, so no network and no Gemini key are needed. The ADK
``LlmAgent`` is wired like The Bouncer but never invoked for question selection
(user decision 2026-10-08: curated bank + local profile).
"""
import pytest

import src.interviewer as interviewer_module
from src.interview_state import (
    InterviewPhase,
    InterviewStateError,
    InterviewStateStore,
    UserProfile,
)
from src.interviewer import (
    ANIMAL_KEYWORD_FAMILIES,
    DEFAULT_INTERVIEWER_MODEL,
    DEFAULT_SUGGESTED_ANIMAL,
    INTERVIEW_QUESTIONS,
    INTERVIEWER_MODEL,
    Interviewer,
    build_profile,
    resolve_model,
    suggest_animal,
)

APP = "telegram-documentaries"

# Copy-lock: the seven curated documentary questions, verbatim.
LOCKED_QUESTIONS = (
    "Right then, let's begin. The moment your alarm goes off, what's the very first thing you do?",
    "Everyone's got a little ritual they'd never admit to. What's yours when nobody's watching?",
    "Walk me through an ordinary weekday — how does it usually unfold, start to finish?",
    "If you could keep only one thing in your life and bin the rest, what stays?",
    "What do you reach for first thing in the morning: food, phone, coffee, or pure chaos?",
    "Be honest — what's the strangest thing you do that makes perfect sense to you and nobody else?",
    "And finally, how do you wind down once the day is done and the cameras are switched off?",
)

ANSWERS = [f"answer number {i}" for i in range(1, 8)]


def _interviewer() -> Interviewer:
    return Interviewer(store=InterviewStateStore())


# --- question bank copy-lock ----------------------------------------------------


def test_question_bank_has_exactly_seven_questions():
    assert len(INTERVIEW_QUESTIONS) == 7


def test_question_bank_copy_is_locked_verbatim():
    assert INTERVIEW_QUESTIONS == LOCKED_QUESTIONS


def test_questions_are_unique():
    assert len(set(INTERVIEW_QUESTIONS)) == 7


# --- start ----------------------------------------------------------------------


def test_start_returns_first_question_and_transitions_to_interviewing():
    interviewer = _interviewer()

    first = interviewer.start(111)

    assert first == INTERVIEW_QUESTIONS[0]
    state = interviewer.state(111)
    assert state.phase is InterviewPhase.INTERVIEWING
    assert state.question_index == 1  # Q1 asked, Q2 is next
    assert state.answers == []
    assert state.profile is None


def test_state_for_unknown_chat_is_idle():
    interviewer = _interviewer()

    state = interviewer.state(999)

    assert state.phase is InterviewPhase.IDLE
    assert state.answers == []


# --- one question at a time & ordering -----------------------------------------


def test_answers_advance_and_store_pairs_in_order():
    interviewer = _interviewer()
    interviewer.start(111)

    interviewer.answer(111, "a1")
    interviewer.answer(111, "a2")

    state = interviewer.state(111)
    assert state.answers == [(INTERVIEW_QUESTIONS[0], "a1"), (INTERVIEW_QUESTIONS[1], "a2")]
    assert state.question_index == 3


def test_each_answer_returns_exactly_one_question():
    interviewer = _interviewer()
    interviewer.start(111)

    for index, answer in enumerate(ANSWERS[:-1]):
        reply = interviewer.answer(111, answer)

        assert reply.completed is False
        assert reply.profile is None
        assert len(reply.messages) == 1, "questions must be asked strictly one at a time"
        assert reply.messages[0] == INTERVIEW_QUESTIONS[index + 1]


def test_questions_arrive_in_bank_order():
    interviewer = _interviewer()
    interviewer.start(111)

    asked = []
    for answer in ANSWERS[:-1]:
        asked.extend(interviewer.answer(111, answer).messages)

    assert asked == list(INTERVIEW_QUESTIONS[1:])


# --- resume ---------------------------------------------------------------------


def test_interrupted_interview_resumes_with_the_right_question():
    """Answer Q1–Q3, walk away, come back: the next text is answered against Q4."""
    interviewer = _interviewer()
    interviewer.start(111)
    interviewer.answer(111, "a1")
    interviewer.answer(111, "a2")
    reply = interviewer.answer(111, "a3")

    assert reply.messages == [INTERVIEW_QUESTIONS[3]]  # Q4 posed; now interrupted

    # A later message resumes the same state and pairs with Q4, then asks Q5.
    resumed = interviewer.answer(111, "a4")

    assert resumed.messages == [INTERVIEW_QUESTIONS[4]]
    assert interviewer.state(111).answers[3] == (INTERVIEW_QUESTIONS[3], "a4")


# --- completion -----------------------------------------------------------------


def _complete(interviewer: Interviewer, chat_id: int):
    interviewer.start(chat_id)
    replies = []
    for answer in ANSWERS:
        replies.append(interviewer.answer(chat_id, answer))
    return replies


def test_seventh_answer_completes_the_interview():
    interviewer = _interviewer()
    replies = _complete(interviewer, 111)

    final = replies[-1]
    assert final.completed is True
    assert final.profile is not None
    state = interviewer.state(111)
    assert state.phase is InterviewPhase.COMPLETE
    assert len(state.answers) == 7
    assert state.profile is not None


def test_completion_reply_is_the_profile_summary():
    interviewer = _interviewer()
    replies = _complete(interviewer, 111)

    assert replies[-1].messages == [replies[-1].profile.summary]


def test_profile_covers_all_four_labels_and_suggested_animal():
    interviewer = _interviewer()
    replies = _complete(interviewer, 111)

    summary = replies[-1].profile.summary
    for label in ("Habits", "Quirks", "Routines", "Preferences"):
        assert f"{label}:" in summary
    assert "Suggested animal:" in summary
    assert replies[-1].profile.suggested_animal in summary


def test_completed_profile_is_stored_on_the_shared_driver():
    store = InterviewStateStore()
    interviewer = Interviewer(store=store)
    _complete(interviewer, 111)

    stored = store.get(111)
    assert stored.phase is InterviewPhase.COMPLETE
    assert stored.profile is not None
    assert stored.profile.chat_id == 111


def test_answer_before_start_is_an_illegal_transition():
    interviewer = _interviewer()

    with pytest.raises(InterviewStateError):
        interviewer.answer(111, "too early")


def test_answer_after_completion_is_an_illegal_transition():
    interviewer = _interviewer()
    _complete(interviewer, 111)

    with pytest.raises(InterviewStateError):
        interviewer.answer(111, "one more")


# --- animal matcher -------------------------------------------------------------


def test_animal_matcher_is_deterministic():
    answers = [(q, "I stay up late at night reading") for q in INTERVIEW_QUESTIONS]

    assert suggest_animal(answers) == suggest_animal(list(answers))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("I'm always up at midnight, total night owl", "the Night Owl"),
        ("I wake before dawn for a morning run", "the Early Bird"),
        ("Weekends are for hiking up a mountain trail", "the Mountain Goat"),
        ("I go swimming in the sea every chance I get", "the Sea Otter"),
        ("I love a party with lots of friends and a crowd", "the Social Butterfly"),
        ("I'm happiest alone and quiet with my own company", "the Lone Wolf"),
        ("I keep a busy, productive work planner", "the Busy Bee"),
        ("I read a book from the library every week", "the Bookworm"),
        ("I nap on the couch and binge a cosy series", "the Dormouse"),
    ],
)
def test_animal_matcher_maps_keyword_families_to_canonical_animals(answer, expected):
    answers = [(INTERVIEW_QUESTIONS[0], answer)]

    assert suggest_animal(answers) == expected


def test_keyword_matcher_uses_whole_words_only():
    """'category' must not match the cat-adjacent keyword 'cat' if it existed;
    more importantly, distinctive words are matched as words, not substrings."""
    answers = [(INTERVIEW_QUESTIONS[0], "I categorise spreadsheets, nothing outdoorsy")]

    assert suggest_animal(answers) == DEFAULT_SUGGESTED_ANIMAL


def test_animal_matcher_falls_back_to_locked_default():
    answers = [(INTERVIEW_QUESTIONS[0], "zzqx blorp fnord 12345")]

    assert suggest_animal(answers) == DEFAULT_SUGGESTED_ANIMAL


def test_keyword_families_are_non_empty():
    assert ANIMAL_KEYWORD_FAMILIES
    for animal, keywords in ANIMAL_KEYWORD_FAMILIES:
        assert animal
        assert keywords


def test_build_profile_is_pure_and_typed():
    answers = list(zip(INTERVIEW_QUESTIONS, ANSWERS))

    profile = build_profile(123, answers)

    assert isinstance(profile, UserProfile)
    assert profile.chat_id == 123
    assert profile.suggested_animal


# --- reset & isolation ----------------------------------------------------------


def test_reset_returns_fresh_idle_state():
    interviewer = _interviewer()
    _complete(interviewer, 111)

    interviewer.reset(111)

    state = interviewer.state(111)
    assert state.phase is InterviewPhase.IDLE
    assert state.answers == []
    assert state.profile is None


def test_reset_is_idempotent():
    interviewer = _interviewer()

    interviewer.reset(111)
    interviewer.reset(111)  # second reset must not raise

    assert interviewer.state(111).phase is InterviewPhase.IDLE


def test_two_chats_never_share_state():
    interviewer = _interviewer()
    interviewer.start(111)
    interviewer.start(222)
    interviewer.answer(111, "a1")
    interviewer.answer(111, "a2")
    interviewer.answer(222, "b1")

    assert len(interviewer.state(111).answers) == 2
    assert len(interviewer.state(222).answers) == 1
    assert interviewer.state(111).answers[0][1] == "a1"
    assert interviewer.state(222).answers[0][1] == "b1"


# --- ADK wiring & model resolution ---------------------------------------------


def test_start_creates_one_in_memory_session_per_chat():
    interviewer = _interviewer()

    interviewer.start(601)

    sessions = interviewer._sessions.list_sessions_sync(app_name=APP, user_id="601").sessions
    assert [s.id for s in sessions] == ["601"]


def test_reset_deletes_only_that_chats_session():
    interviewer = _interviewer()
    interviewer.start(701)
    interviewer.start(702)

    interviewer.reset(701)

    assert interviewer._sessions.list_sessions_sync(app_name=APP, user_id="701").sessions == []
    remaining = interviewer._sessions.list_sessions_sync(app_name=APP, user_id="702").sessions
    assert [s.id for s in remaining] == ["702"]


def test_default_model_is_gemini_flash_lite():
    assert DEFAULT_INTERVIEWER_MODEL == "gemini-3.1-flash-lite"


def test_resolve_model_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("INTERVIEWER_MODEL", raising=False)

    assert resolve_model() == "gemini-3.1-flash-lite"


def test_resolve_model_honours_env_override(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_MODEL", "gemini-9.9-test-override")

    assert interviewer_module.resolve_model() == "gemini-9.9-test-override"
