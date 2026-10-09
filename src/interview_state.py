"""Interview state driver (ROADMAP Phase 3).

The single owner of per-chat interview state: versioned, typed, in-memory, and
keyed by ``chat_id`` (int). ``SPECS/TECH.md`` session-state rules live here:

* one shared driver owns all reads/writes — callers never mutate state directly;
* unknown chat → fresh ``IDLE`` state (no exception);
* corrupt / foreign state or a wrong-typed ``chat_id`` → **loud error**, never a
  silent fallback;
* scheme is versioned (``schema_version``) so old sessions fail predictably.

``chat_id`` is typed ``int`` everywhere. A str-vs-int drift would silently break
per-chat lookups (a named category in the spec), so the driver rejects it.

Threading model: the app is a **single-threaded long-polling loop** (one update
is dispatched at a time), so the Interviewer's get → mutate → save round trip
on a copied state is effectively atomic in practice. The store's lock protects
the dict itself (isolation of individual reads/writes); it is not a transaction
lock over multi-call sequences.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class InterviewStateError(RuntimeError):
    """Raised on an illegal transition or corrupt/foreign stored state."""


# Current per-chat schema. v1 (pre-Phase-5) had no ``script`` field; records are
# migrated forward losslessly on read/write and any newer version fails loud.
SCHEMA_VERSION = 2


class InterviewPhase(str, Enum):
    """Explicit interview state machine (TECH.md: named phases)."""

    IDLE = "idle"  # awaiting a photo (gate) — text gets "Hi Mate"
    INTERVIEWING = "interviewing"  # awaiting the answer to question N
    COMPLETE = "complete"  # profile produced; kept for the next stage


def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string (state's ``updated_at``)."""
    return datetime.now(timezone.utc).isoformat()


class UserProfile(BaseModel):
    """The typed hand-off to the next pipeline stage (Converter)."""

    chat_id: int
    summary: str  # full profile text incl. Habits/Quirks/Routines/Preferences
    suggested_animal: str  # clearly identified animal for the Converter


class InterviewState(BaseModel):
    """One chat's interview progress. Strict: untrusted/foreign shapes fail loud."""

    model_config = ConfigDict(strict=True, extra="forbid")

    chat_id: int
    phase: InterviewPhase
    question_index: int = 0  # 0-based, next question to ask
    answers: list[tuple[str, str]] = Field(default_factory=list)  # (question, answer)
    profile: UserProfile | None = None
    script: str | None = None  # Phase 5 hand-off to the Narrator (raw script text)
    schema_version: int = SCHEMA_VERSION
    updated_at: str  # ISO-8601 UTC


class InterviewStateStore:
    """In-memory, thread-safe, per-``chat_id`` interview state store."""

    def __init__(self) -> None:
        self._states: dict[int, InterviewState] = {}
        self._lock = threading.Lock()

    def get(self, chat_id: int) -> InterviewState:
        """Return chat's state.

        Unknown chat → fresh ``IDLE`` state. A stored value that is not a valid
        ``InterviewState``, belongs to a different chat, or carries an unknown
        ``schema_version`` is a loud error. The returned object is a copy — the
        store remains the single writer.

        Note: this class is intended for the app's single-threaded polling loop;
        the lock guards individual reads/writes, not multi-call transactions.
        """
        self._require_int_chat_id(chat_id)
        with self._lock:
            if chat_id not in self._states:
                return self._fresh_state(chat_id)
            state = self._states[chat_id]
            normalized = self._normalize(state, chat_id)
            if normalized is not state:
                # Persist the forward migration so the store stays at the
                # current schema (and re-reads no longer need to migrate).
                self._states[chat_id] = normalized
        return normalized.model_copy(deep=True)

    def save(self, chat_id: int, state: InterviewState) -> None:
        """Persist one chat's state (a copy). Wrong type / mismatched key raise."""
        self._require_int_chat_id(chat_id)
        if not isinstance(state, InterviewState):
            raise InterviewStateError(
                f"refusing to save {type(state).__name__} as interview state"
            )
        if state.chat_id != chat_id:
            raise InterviewStateError(
                f"refusing to save chat {chat_id} state under key {state.chat_id}"
            )
        normalized = self._normalize(state, chat_id)
        with self._lock:
            self._states[chat_id] = normalized.model_copy(deep=True)

    def delete(self, chat_id: int) -> None:
        """Purge one chat's state. Idempotent and chat-scoped."""
        self._require_int_chat_id(chat_id)
        with self._lock:
            self._states.pop(chat_id, None)

    @staticmethod
    def _normalize(state: InterviewState, chat_id: int) -> InterviewState:
        """Validate a stored/saved record and migrate older schemas forward.

        Only known versions are interpreted: a v1 record (pre-Phase-5, no
        ``script``) migrates losslessly to v2 with ``script=None`` and a logged
        ``event=state_migrated``; a version **newer** than the current schema
        (or an unknown older one) fails loud so old sessions fail predictably
        rather than misbehave (TECH.md).
        """
        if not isinstance(state, InterviewState):
            raise InterviewStateError(
                f"corrupt interview state for chat_id={chat_id}: "
                f"expected InterviewState, found {type(state).__name__}"
            )
        if state.chat_id != chat_id:
            raise InterviewStateError(
                f"corrupt interview state: key {chat_id} holds chat_id={state.chat_id}"
            )
        version = state.schema_version
        if version == SCHEMA_VERSION:
            return state
        if version == 1:
            migrated = state.model_copy(
                update={"schema_version": SCHEMA_VERSION, "script": None}
            )
            logger.info(
                "event=state_migrated chat_id=%d from_version=%d to_version=%d",
                chat_id,
                version,
                SCHEMA_VERSION,
            )
            return migrated
        raise InterviewStateError(
            f"corrupt interview state for chat_id={chat_id}: unsupported "
            f"schema_version={version} (current {SCHEMA_VERSION})"
        )

    @staticmethod
    def _fresh_state(chat_id: int) -> InterviewState:
        return InterviewState(
            chat_id=chat_id,
            phase=InterviewPhase.IDLE,
            updated_at=utc_now_iso(),
        )

    @staticmethod
    def _require_int_chat_id(chat_id: int) -> None:
        # bool is a subclass of int; a bool chat_id is still wrong-typed input.
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise InterviewStateError(
                f"chat_id must be int, got {type(chat_id).__name__}"
            )
