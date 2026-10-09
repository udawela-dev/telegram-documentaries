"""The Scripter — narration (ROADMAP Phase 5).

Pipeline position: Telegram → Bouncer → Interviewer → Converter → **Scripter**
(this module) → Narrator.

An ADK ``LlmAgent`` on **Gemini 3.1 Flash Lite** turns the completed
:class:`UserProfile` (habits / routines / quirks + suggested animal) into exactly
one dramatic British-wildlife-documentary paragraph (60–90 words, TTS-ready, no
markdown, no extra commentary). The validated paragraph is stored as a raw
string on the shared :class:`InterviewState` driver for the Phase 6 Narrator and
returned so the gateway can send it to the chat.

Shape enforcement mirrors the Interviewer/Converter house pattern:

* ``validate_script`` is the **single deterministic gate** for both generation
  paths (one paragraph, 60–90 words, markdown sniff);
* Gemini runs first; on failure / timeout / blocked key **or a shape-rejected
  output** the key-free :class:`LocalScriptWriter` produces a valid paragraph;
* with neither available it raises a loud :class:`ScripterError` (never a
  fake/off-spec "script") and the gateway degrades gracefully.

A hard timeout (``SCRIPTER_GEMINI_TIMEOUT``, default 60s) runs the LLM work on a
daemon thread behind a ``threading.Event``, mirroring The Bouncer/Converter. ADK
sessions are strictly per-call (fresh delete-then-create + a ``finally`` reap):
a reused session would leak the previous prompt/paragraph as history.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from src.interview_state import (
    SCHEMA_VERSION,
    InterviewState,
    InterviewStateStore,
    UserProfile,
    utc_now_iso,
)
from src.logging_utils import log_call
from src.persona import PERSONA_SCRIPT_TONE, Persona

if TYPE_CHECKING:  # avoids any runtime dependency for importers
    from src.local_script import LocalScriptWriter

logger = logging.getLogger(__name__)

APP_NAME = "telegram-documentaries"

DEFAULT_SCRIPTER_MODEL = "gemini-3.1-flash-lite"
# Import-time default (operators/tests); resolve_model() re-reads the env at call
# time so a running deployment's SCRIPTER_MODEL override actually applies.
SCRIPTER_MODEL = os.getenv("SCRIPTER_MODEL", DEFAULT_SCRIPTER_MODEL)

# Timeout for one Gemini narration attempt. Read at call time so tests can
# shrink it; on expiry the local writer produces the paragraph instead.
DEFAULT_GEMINI_TIMEOUT_SECONDS = 60.0

# The locked output contract: exactly one paragraph within this word budget.
SCRIPTER_MIN_WORDS = 60
SCRIPTER_MAX_WORDS = 90

# Locked copy used by the gateway when the scripter cannot run.
SCRIPTER_REPLY_UNAVAILABLE = (
    "Hang on — the narrator's quill ran dry. Give that another go?"
)

INSTRUCTION = (
    "You are The Scripter, the narrator of a comedy wildlife-documentary "
    "Telegram bot. From the subject's behavioural dossier, write the opening "
    "narration: one warm, playful, dramatic British-wildlife-documentary "
    "paragraph. Return only that paragraph."
)

# Markdown markers that must never reach TTS. One paragraph means no blank
# line; single newlines are collapsed as whitespace.
_MARKDOWN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\*\*"),  # bold
    re.compile(r"__"),  # bold / underscores
    re.compile(r"`"),  # inline or fenced code
    re.compile(r"(?m)^\s{0,3}#{1,6}\s"),  # ATX heading
    re.compile(r"(?m)^\s{0,3}[-*+]\s"),  # bullet list
    re.compile(r"(?m)^\s{0,3}\d+[.)]\s"),  # ordered list
    re.compile(r"(?m)^\s{0,3}(?:[-*_]\s*){3,}$"),  # horizontal rule
    re.compile(r"(?m)^\s{0,3}>"),  # blockquote
    re.compile(r"\[[^\]]+\]\([^)]+\)"),  # link
    re.compile(r"(?<!\*)\*(?!\*)"),  # single-asterisk emphasis
    re.compile(r"(?<!_)_(?!_)"),  # single-underscore emphasis
)


def resolve_model() -> str:
    """Resolve the Scripter's Gemini model at call time — env override wins."""
    return os.getenv("SCRIPTER_MODEL", DEFAULT_SCRIPTER_MODEL)


def persona_instruction(persona: Persona) -> str:
    """Compose the run's instruction: the persona's tone prepended to the base.

    The base :data:`INSTRUCTION` is unchanged by persona (only a leading tone
    snippet is added), so the Scripter's task contract is identical for both
    presenters. Rejects free text: only a typed :class:`Persona` is accepted.
    """
    if not isinstance(persona, Persona):
        raise ScripterError(f"unknown persona: {persona!r}")
    return f"{PERSONA_SCRIPT_TONE[persona]}\n\n{INSTRUCTION}"


def validate_script(text: str) -> str | None:
    """Deterministically validate one script against the locked shape contract.

    Collapses whitespace, requires exactly one paragraph block and 60–90 words,
    and sniffs out markdown. Returns the normalized single-paragraph text when
    valid, ``None`` when the contract is violated (never raises, never assumes).
    """
    if not isinstance(text, str):
        return None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not lines:
        return None
    # More than one paragraph block (a blank line) is an immediate rejection.
    if re.search(r"\n[ \t]*\n", lines):
        return None
    if any(pattern.search(lines) for pattern in _MARKDOWN_PATTERNS):
        return None
    normalized = " ".join(lines.split())
    word_count = len(normalized.split(" "))
    if not SCRIPTER_MIN_WORDS <= word_count <= SCRIPTER_MAX_WORDS:
        return None
    return normalized


class ScripterError(RuntimeError):
    """Raised when no valid script could be produced or the store rejected a write."""


class Scripter:
    """ADK narration agent + key-free local-writer fallback."""

    APP_NAME = APP_NAME
    # Exposed on the class (and used by ``_llm_bounded``) so subclasses/tests can
    # pin or override the locked default without reaching for the module global.
    DEFAULT_GEMINI_TIMEOUT_SECONDS = DEFAULT_GEMINI_TIMEOUT_SECONDS

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        store: InterviewStateStore | None = None,
        local_writer: LocalScriptWriter | None = None,
    ) -> None:
        # ADK's genai client resolves GEMINI_API_KEY from the environment; make
        # the one from our settings visible without ever hardcoding it.
        if api_key:
            os.environ.setdefault("GEMINI_API_KEY", api_key)
        # Resolved at construction time so a runtime SCRIPTER_MODEL override is
        # honoured (never cached at import).
        self._model = model or resolve_model()
        self._store = store or InterviewStateStore()
        self._local = local_writer
        # Gemini is only attempted when a key is actually in view; otherwise the
        # Scripter is local-only (no wasted round-trip to a keyless client).
        self._has_gemini = bool(api_key or os.environ.get("GEMINI_API_KEY"))
        self._agent = LlmAgent(name="scripter", model=self._model, instruction=INSTRUCTION)
        self._sessions = InMemorySessionService()
        self._runner = Runner(
            agent=self._agent,
            app_name=APP_NAME,
            session_service=self._sessions,
        )

    @property
    def model(self) -> str:
        return self._model

    def _session_id(self, chat_id: int) -> str:
        """One in-memory session = one chat_id (string form is right-sized)."""
        return str(chat_id)

    def _delete_session(self, chat_id: int) -> None:
        """Remove the chat's session; idempotent (mirrors ``Bouncer.reset_chat``)."""
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

    def reset_chat(self, chat_id: int) -> None:
        """Purge the chat's ADK session (called on resets). Idempotent and chat-scoped."""
        self._delete_session(chat_id)
        logger.info("event=scripter_session_reset chat_id=%d", chat_id)

    def _fresh_session(self, chat_id: int) -> str:
        """Return a brand-new per-chat session, discarding any previous one.

        Every ``write_script`` is exactly one text turn: a reused session would
        ship the previous prompt/paragraph as history to the model and keep the
        text in memory forever. Deleting first makes the single-call contract
        hold on every write, whatever path led here.
        """
        self._delete_session(chat_id)
        session_id = self._session_id(chat_id)
        self._sessions.create_session_sync(
            app_name=APP_NAME, user_id=session_id, session_id=session_id
        )
        return session_id

    def _prompt_content(self, profile: UserProfile) -> genai_types.Content:
        """Assemble the single text turn prompting the narration paragraph."""
        prompt = (
            "Using the subject's behavioural profile below, write exactly one "
            "paragraph of 60–90 words as a single dramatic "
            "British-wildlife-documentary narration. Ground it in the suggested "
            "animal. Use one paragraph only, and no markdown.\n\n"
            f"{profile.summary}\n\n"
            f"Suggested animal: {profile.suggested_animal}."
        )
        return genai_types.Content(
            role="user", parts=[genai_types.Part(text=prompt)]
        )

    def _collect_final_text(self, content: genai_types.Content, session_id: str) -> str:
        """Run the ADK agent once and return the final response's text."""
        texts: list[str] = []
        for event in self._runner.run(
            user_id=session_id, session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    text = getattr(part, "text", None)
                    if text:
                        texts.append(text)
        return " ".join(texts)

    def _run_llm(self, content: genai_types.Content, session_id: str) -> str:
        """Run the narration agent and return the generated text.

        Scriptable seam for offline tests (returns text, not bytes).
        """
        return self._collect_final_text(content, session_id)

    def _llm_bounded(self, content: genai_types.Content, session_id: str) -> str:
        """Run the LLM step with a hard timeout (The Bouncer's pattern).

        A hung Gemini request must not freeze the whole bot: past
        ``SCRIPTER_GEMINI_TIMEOUT`` the orphaned worker is abandoned (daemon
        thread) and a ``TimeoutError`` is raised, which the generation path
        turns into a local-writer fallback.
        """
        timeout = float(
            os.getenv(
                "SCRIPTER_GEMINI_TIMEOUT", str(self.DEFAULT_GEMINI_TIMEOUT_SECONDS)
            )
        )
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def _run() -> None:
            try:
                outcome["value"] = self._run_llm(content, session_id)
            except BaseException as exc:  # noqa: BLE001 — re-raised verbatim below
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=_run, name="scripter-gemini", daemon=True)
        worker.start()
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"scripter gemini call exceeded {timeout:.0f}s") from None
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def _generate(
        self, chat_id: int, profile: UserProfile, session_id: str, persona: Persona
    ) -> tuple[str, str]:
        """Return ``(validated_script, source)``; Gemini first, local fallback.

        A shape-rejected LLM output is treated exactly like an LLM failure: the
        local writer produces the paragraph instead and an off-spec text is
        never returned or stored. ``persona`` colours both paths (the ADK agent
        instruction for Gemini, the opener for the local writer).
        """
        # ADK resolves ``canonical_instruction`` per run, so setting it here
        # (before the runner starts) applies persona tone to this run only.
        self._agent.instruction = persona_instruction(persona)
        if self._has_gemini:
            try:
                text = self._llm_bounded(self._prompt_content(profile), session_id)
                script = validate_script(text)
                if script is not None:
                    return script, "gemini"
                logger.warning(
                    "event=script_validation_failed chat_id=%d source=gemini", chat_id
                )
            except Exception as exc:  # noqa: BLE001 — any LLM failure → local fallback
                logger.warning(
                    "event=scripter_gemini_failed chat_id=%d error_type=%s error=%.120r fallback_local=%s",
                    chat_id,
                    type(exc).__name__,
                    exc,
                    self._local is not None,
                )

        if self._local is None:
            raise ScripterError(
                f"scripter unavailable for chat {chat_id}: no script from Gemini "
                "and no local writer wired"
            )

        try:
            text = self._local.write(profile, persona)
        except Exception as exc:
            raise ScripterError(
                f"local script writer failed for chat {chat_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        script = validate_script(text)
        if script is None:
            logger.error(
                "event=script_validation_failed chat_id=%d source=local", chat_id
            )
            raise ScripterError(
                f"local script writer produced an off-spec script for chat {chat_id}"
            )
        return script, "local"

    def _store_script(self, chat_id: int, script: str) -> None:
        """Persist the raw script on the shared driver, preserving other fields."""
        try:
            state = self._store.get(chat_id)
            updated: InterviewState = state.model_copy(
                update={
                    "script": script,
                    "schema_version": SCHEMA_VERSION,
                    "updated_at": utc_now_iso(),
                }
            )
            self._store.save(chat_id, updated)
        except Exception as exc:
            raise ScripterError(
                f"could not store script for chat {chat_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    @log_call(event="scripter_write_script")
    def write_script(
        self, chat_id: int, profile: UserProfile, persona: Persona = Persona.ATTENBOROUGH
    ) -> str:
        """Produce, validate and store one narration paragraph for a chat.

        Gemini first; on any failure/timeout/blocked key or a rejected shape the
        key-free local writer (if wired) produces the paragraph. With neither
        available — or the store rejecting the write — raises ``ScripterError``.
        The raw string is stored on the shared ``InterviewState`` for Phase 6.

        ``persona`` colours the narration tone; it defaults to
        ``attenborough`` so callers that never resolved a persona keep the
        Phase 7 behaviour exactly.
        """
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ScripterError(f"chat_id must be int, got {type(chat_id).__name__}")
        if not isinstance(profile, UserProfile):
            raise ScripterError(f"write_script requires a UserProfile for chat {chat_id}")
        if not isinstance(persona, Persona):
            raise ScripterError(f"unknown persona: {persona!r}")

        session_id = self._fresh_session(chat_id)
        try:
            script, source = self._generate(chat_id, profile, session_id, persona)
        finally:
            # The narration is one-shot: drop the session so neither the prompt
            # nor the paragraph stays in memory for the chat's life.
            self._delete_session(chat_id)

        self._store_script(chat_id, script)
        logger.info("event=script_generated chat_id=%d source=%s", chat_id, source)
        logger.info(
            "event=script_stored chat_id=%d words=%d", chat_id, len(script.split())
        )
        return script
