"""The Interviewer — stateful orchestrator for Phase 3 (ROADMAP Phase 3).

Pipeline position: Telegram → Bouncer (photo gate) → **Interviewer** (this
module) → Converter (Phase 4).

Design decision (SPECS/2026-10-08-interviewer/requirements.md, 2026-10-08):
Gemini is API-key-blocked until a healthy key exists, so the agent must be fully
functional in Telegram today. An ADK ``LlmAgent`` on ``gemini-3.1-flash-lite``
(with one ``InMemorySessionService`` session per chat) is wired as the agent
backbone exactly mirroring :mod:`src.bouncer`, but question selection and profile
generation are **deterministic, offline, and testable**:

* the seven questions are a curated, copy-locked bank (``INTERVIEW_QUESTIONS``);
* answers are stored in order on the shared :class:`InterviewStateStore`;
* after the seventh answer a behavioural profile (Habits / Quirks / Routines /
  Preferences) plus a clearly identified suggested animal is built by a local,
  rule-based matcher.

Illegal transitions (answering before the interview starts, or after it has
completed) fail loud; they never silently mutate state.
"""

from __future__ import annotations

import logging
import os
import re

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from pydantic import BaseModel

from src.interview_state import (
    InterviewPhase,
    InterviewState,
    InterviewStateError,
    InterviewStateStore,
    UserProfile,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

APP_NAME = "telegram-documentaries"

DEFAULT_INTERVIEWER_MODEL = "gemini-3.1-flash-lite"
# Import-time default (operators/tests); resolve_model() re-reads the env at call
# time so a running deployment's INTERVIEWER_MODEL override actually applies.
INTERVIEWER_MODEL = os.getenv("INTERVIEWER_MODEL", DEFAULT_INTERVIEWER_MODEL)


def resolve_model() -> str:
    """Resolve the Interviewer's Gemini model at call time — env override wins."""
    return os.getenv("INTERVIEWER_MODEL", DEFAULT_INTERVIEWER_MODEL)


# Locked copy (requirements.md "Locked decisions"): seven playful, investigative,
# documentary-style questions asked strictly one at a time.
INTERVIEW_QUESTIONS: tuple[str, ...] = (
    "Right then, let's begin. The moment your alarm goes off, what's the very first thing you do?",
    "Everyone's got a little ritual they'd never admit to. What's yours when nobody's watching?",
    "Walk me through an ordinary weekday — how does it usually unfold, start to finish?",
    "If you could keep only one thing in your life and bin the rest, what stays?",
    "What do you reach for first thing in the morning: food, phone, coffee, or pure chaos?",
    "Be honest — what's the strangest thing you do that makes perfect sense to you and nobody else?",
    "And finally, how do you wind down once the day is done and the cameras are switched off?",
)

# Reply copy used by the gateway when the Interviewer cannot be reached / when a
# chat is reset. Business logic never lives in these strings.
INTERVIEWER_REPLY_UNAVAILABLE = (
    "Hang on — I've lost my notes for a moment. Give that another go?"
)
INTERVIEWER_REPLY_RESET = (
    "Righto — slate wiped clean. Send me a photo and we'll start your documentary."
)

# Profile labels (copy-locked): the four behavioural dimensions plus the clearly
# identified suggested animal.
PROFILE_LABELS: tuple[str, ...] = ("Habits", "Quirks", "Routines", "Preferences")
SUGGESTED_ANIMAL_PREFIX = "Suggested animal:"

# Deterministic animal matcher (requirements.md "Locked decisions"). Families are
# ordered by priority; the first keyword hit wins. Whole-word matching avoids
# substring false positives.
ANIMAL_KEYWORD_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("the Night Owl", ("night", "midnight", "nocturnal", "insomnia")),
    ("the Early Bird", ("morning", "early", "dawn", "sunrise")),
    ("the Mountain Goat", ("hike", "hiking", "mountain", "climb", "climbing", "trail", "outdoors")),
    ("the Sea Otter", ("swim", "swimming", "sea", "ocean", "beach", "water")),
    ("the Social Butterfly", ("friends", "social", "party", "crowd", "outgoing", "people")),
    ("the Lone Wolf", ("alone", "solitary", "solo", "independent", "introvert", "quiet")),
    ("the Busy Bee", ("work", "busy", "productive", "organised", "organized", "planner")),
    ("the Bookworm", ("read", "reading", "book", "books", "library", "study", "learning")),
    ("the Dormouse", ("nap", "sleep", "couch", "binge", "relax", "cozy", "cosy")),
)

# Locked fallback when no keyword family matches.
DEFAULT_SUGGESTED_ANIMAL = "the House Cat"

# Each question maps to one profile dimension. Q1–Q7 → 0,1,2,3,0,1,2 so all four
# labels are populated by a completed interview.
_QUESTION_TO_LABEL = ("Habits", "Quirks", "Routines", "Preferences", "Habits", "Quirks", "Routines")

# The question bank and its profile-label map must never drift apart.
assert len(INTERVIEW_QUESTIONS) == len(_QUESTION_TO_LABEL), (
    "INTERVIEW_QUESTIONS and _QUESTION_TO_LABEL must stay in lockstep"
)

# Whole-word keyword patterns compiled once at import (one alternation per family
# instead of re-compiling per keyword per call). ``ANIMAL_KEYWORD_FAMILIES`` stays
# the source of truth for tests and readers; this is just the fast lookup form.
_ANIMAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (
        animal,
        re.compile(rf"\b(?:{'|'.join(re.escape(keyword) for keyword in keywords)})\b"),
    )
    for animal, keywords in ANIMAL_KEYWORD_FAMILIES
)

INSTRUCTION = (
    "You are The Interviewer, the orchestral heart of a wildlife-documentary "
    "Telegram bot. You conduct a short, playful, slightly eccentric interview "
    "with a human subject — one question at a time — gathering their habits, "
    "quirks, routines and preferences so a later stage can portray them as a "
    "creature in the wild. Stay warm, curious and gently comedic; never ask two "
    "questions in one turn."
)


class InterviewReply(BaseModel):
    """Result of storing one answer: the next message(s) and completion data."""

    messages: list[str]
    completed: bool = False
    profile: UserProfile | None = None


def suggest_animal(answers: list[tuple[str, str]]) -> str:
    """Deterministically match stored answers to a canonical animal.

    Whole-word keyword families over the concatenated answers, first family in
    priority order wins; a locked default when nothing matches.
    """
    text = " ".join(answer for _question, answer in answers).lower()
    for animal, pattern in _ANIMAL_PATTERNS:
        if pattern.search(text):
            return animal
    return DEFAULT_SUGGESTED_ANIMAL


def build_profile(chat_id: int, answers: list[tuple[str, str]]) -> UserProfile:
    """Build the typed behavioural profile from the stored ``(question, answer)``s."""
    buckets: dict[str, list[str]] = {label: [] for label in PROFILE_LABELS}
    for index, (_question, answer) in enumerate(answers):
        label = _QUESTION_TO_LABEL[index % len(_QUESTION_TO_LABEL)]
        cleaned = (answer or "").strip()
        if cleaned:
            buckets[label].append(cleaned)

    animal = suggest_animal(answers)
    lines = ["Personality profile:"]
    for label in PROFILE_LABELS:
        content = "; ".join(buckets[label]) if buckets[label] else "nothing recorded"
        lines.append(f"{label}: {content}")
    lines.append(f"{SUGGESTED_ANIMAL_PREFIX} {animal}")
    return UserProfile(
        chat_id=chat_id,
        summary="\n".join(lines),
        suggested_animal=animal,
    )


class Interviewer:
    """ADK agent backbone + local deterministic interview state machine."""

    APP_NAME = APP_NAME

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        store: InterviewStateStore | None = None,
    ) -> None:
        # ADK's genai client resolves GEMINI_API_KEY from the environment; make
        # the one from our settings visible without ever hardcoding it.
        if api_key:
            os.environ.setdefault("GEMINI_API_KEY", api_key)
        # Resolved at construction time so a runtime INTERVIEWER_MODEL override
        # is honoured (never cached at import).
        self._model = model or resolve_model()
        self._store = store or InterviewStateStore()
        # ADK wiring mirrors The Bouncer (agent + per-chat sessions + a Runner).
        # The Runner is intentionally DORMANT in this phase: question selection
        # and profile generation are deterministic and local (Gemini-key-free),
        # so the agent is never invoked — it is the backbone for later phases
        # once a healthy Gemini key exists.
        self._agent = LlmAgent(name="interviewer", model=self._model, instruction=INSTRUCTION)
        self._sessions = InMemorySessionService()
        self._runner = Runner(
            agent=self._agent,
            app_name=APP_NAME,
            session_service=self._sessions,
        )
        logger.info(
            "event=interviewer_ready model=%s local_first=true", self._model
        )

    @property
    def model(self) -> str:
        return self._model

    def _session_id(self, chat_id: int) -> str:
        """One in-memory session = one chat_id (string form is right-sized)."""
        return str(chat_id)

    def _ensure_session(self, chat_id: int) -> str:
        session_id = self._session_id(chat_id)
        if (
            self._sessions.get_session_sync(
                app_name=APP_NAME, user_id=session_id, session_id=session_id
            )
            is None
        ):
            self._sessions.create_session_sync(
                app_name=APP_NAME, user_id=session_id, session_id=session_id
            )
        return session_id

    def _delete_session(self, chat_id: int) -> None:
        session_id = self._session_id(chat_id)
        if (
            self._sessions.get_session_sync(
                app_name=APP_NAME, user_id=session_id, session_id=session_id
            )
            is not None
        ):
            self._sessions.delete_session_sync(
                app_name=APP_NAME, user_id=session_id, session_id=session_id
            )

    def state(self, chat_id: int) -> InterviewState:
        """The chat's current state (fresh IDLE for an unknown chat)."""
        return self._store.get(chat_id)

    def start(self, chat_id: int) -> str:
        """Begin a fresh interview for ``chat_id`` and return the first question.

        An approved photo is the entry point, so this always opens a clean
        interview (IDLE → INTERVIEWING); any earlier progress is superseded.
        """
        self._ensure_session(chat_id)
        state = InterviewState(
            chat_id=chat_id,
            phase=InterviewPhase.INTERVIEWING,
            question_index=1,  # Q1 asked; Q2 is next
            answers=[],
            profile=None,
            updated_at=utc_now_iso(),
        )
        self._store.save(chat_id, state)
        logger.info("event=interview_started chat_id=%d", chat_id)
        return INTERVIEW_QUESTIONS[0]

    def answer(self, chat_id: int, text: str) -> InterviewReply:
        """Store one answer; return exactly one next question, or the profile.

        The question being answered is the last one posed
        (``INTERVIEW_QUESTIONS[question_index - 1]``) so an interrupted interview
        resumes against the right question without any extra bookkeeping.
        """
        state = self._store.get(chat_id)
        if state.phase is not InterviewPhase.INTERVIEWING:
            raise InterviewStateError(
                f"cannot accept an answer for chat {chat_id} in phase "
                f"{state.phase.value}"
            )

        answered_index = state.question_index - 1
        if not 0 <= answered_index < len(INTERVIEW_QUESTIONS):
            raise InterviewStateError(
                f"chat {chat_id} has no pending question (index {answered_index})"
            )

        question = INTERVIEW_QUESTIONS[answered_index]
        state.answers.append((question, text))
        logger.info(
            "event=interview_answer_stored chat_id=%d question_index=%d",
            chat_id,
            answered_index,
        )

        if len(state.answers) >= len(INTERVIEW_QUESTIONS):
            profile = build_profile(chat_id, state.answers)
            state.profile = profile
            state.phase = InterviewPhase.COMPLETE
            state.updated_at = utc_now_iso()
            self._store.save(chat_id, state)
            logger.info(
                "event=interview_completed chat_id=%d animal=%s",
                chat_id,
                profile.suggested_animal,
            )
            return InterviewReply(messages=[profile.summary], completed=True, profile=profile)

        next_index = state.question_index  # index of the question to ask next
        state.question_index = next_index + 1
        state.updated_at = utc_now_iso()
        self._store.save(chat_id, state)
        return InterviewReply(messages=[INTERVIEW_QUESTIONS[next_index]])

    def reset(self, chat_id: int) -> None:
        """Purge the chat's interview state and ADK session. Idempotent."""
        self._store.delete(chat_id)
        self._delete_session(chat_id)
        logger.info("event=interview_reset chat_id=%d", chat_id)
