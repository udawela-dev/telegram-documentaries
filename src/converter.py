"""The Converter — hybrid portrait (ROADMAP Phase 4).

Pipeline position: Telegram → Bouncer → Interviewer → **Converter** (this
module) → Scripter → Narrator.

An ADK ``LlmAgent`` on **Gemini 3.1 Flash Image** fuses the original portrait
with the interview dossier into a hybrid animal portrait, returned directly to
Telegram with no intermediate text hop (``SPECS/TECH.md``):

* exactly **one multimodal call** — a single ``Content`` whose parts are
  ``[Blob(portrait bytes), Part(text=prompt)]``; the prompt embeds the profile
  summary + suggested animal + art direction;
* ``generate_content_config`` requests ``response_modalities=["IMAGE"]``;
* the generated image is extracted from the final response's
  ``part.inline_data.data``; text-only / empty / malformed output raises a loud
  :class:`ConverterError` (never a silent fake "image");
* a hard timeout (``CONVERTER_GEMINI_TIMEOUT``, default 60s) runs the LLM work
  on a daemon thread behind a ``threading.Event``, mirroring The Bouncer.

Resilience: on any LLM failure/timeout/blocked key the Converter falls back to
the key-free :class:`LocalHybridComposer` (if wired), so the pipeline still
emits a real image today. With neither the LLM nor a composer available it
raises ``ConverterError`` and the gateway degrades gracefully.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from src.interview_state import UserProfile
from src.logging_utils import log_call

if TYPE_CHECKING:  # avoids a hard runtime cv2 dependency for importers
    from src.local_composite import LocalHybridComposer

logger = logging.getLogger(__name__)

APP_NAME = "telegram-documentaries"

DEFAULT_CONVERTER_MODEL = "gemini-3.1-flash-image"
# Import-time default (operators/tests); resolve_model() re-reads the env at call
# time so a running deployment's CONVERTER_MODEL override actually applies.
CONVERTER_MODEL = os.getenv("CONVERTER_MODEL", DEFAULT_CONVERTER_MODEL)

# Timeout for one Gemini image-generation attempt. Read at call time so tests
# can shrink it; on expiry the local composer produces the image instead.
DEFAULT_GEMINI_TIMEOUT_SECONDS = 60.0

# Locked copy used by the gateway when the converter cannot run.
CONVERTER_REPLY_UNAVAILABLE = (
    "Hang on — I couldn't quite finish your portrait. Give that another go?"
)

INSTRUCTION = (
    "You are The Converter, the portrait artist of a comedy wildlife-documentary "
    "Telegram bot. Given the subject's photograph and their behavioural dossier, "
    "produce ONE photorealistic hybrid animal portrait: the subject's own face "
    "blended seamlessly with the suggested animal. Keep it warm, playful and "
    "photo-booth scale. Return only the finished image."
)


def resolve_model() -> str:
    """Resolve the Converter's Gemini model at call time — env override wins."""
    return os.getenv("CONVERTER_MODEL", DEFAULT_CONVERTER_MODEL)


class ConverterError(RuntimeError):
    """Raised when no image could be produced (bad input, no image in response)."""


class Converter:
    """ADK image-generation agent + key-free local-composer fallback."""

    APP_NAME = APP_NAME

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        local_composer: "LocalHybridComposer | None" = None,
    ) -> None:
        # ADK's genai client resolves GEMINI_API_KEY from the environment; make
        # the one from our settings visible without ever hardcoding it.
        if api_key:
            os.environ.setdefault("GEMINI_API_KEY", api_key)
        # Resolved at construction time so a runtime CONVERTER_MODEL override is
        # honoured (never cached at import).
        self._model = model or resolve_model()
        self._local = local_composer
        # Gemini is only attempted when a key is actually in view; otherwise the
        # Converter is local-only (no wasted round-trip to a keyless client).
        self._has_gemini = bool(api_key or os.environ.get("GEMINI_API_KEY"))
        self._agent = LlmAgent(
            name="converter",
            model=self._model,
            instruction=INSTRUCTION,
            generate_content_config=genai_types.GenerateContentConfig(
                response_modalities=["IMAGE"]
            ),
        )
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

    def _fresh_session(self, chat_id: int) -> str:
        """Return a brand-new per-chat session, discarding any previous one.

        Every ``hybridize`` is exactly one multimodal turn: a reused session
        would ship the previous portrait/prompt/image as inline history to the
        model and keep those bytes in memory forever. Deleting first makes the
        single-call contract hold on every conversion, whatever path led here.
        """
        self._delete_session(chat_id)
        session_id = self._session_id(chat_id)
        self._sessions.create_session_sync(
            app_name=APP_NAME, user_id=session_id, session_id=session_id
        )
        return session_id

    def reset_chat(self, chat_id: int) -> None:
        """Purge the chat's ADK session (called on ``/start``, ``/restart`` and
        rejected photos, beside the Bouncer/Interviewer resets). Idempotent and
        chat-scoped; never touches another chat."""
        self._delete_session(chat_id)
        logger.info("event=converter_session_reset chat_id=%d", chat_id)

    def _prompt_content(self, portrait: bytes, profile: UserProfile) -> genai_types.Content:
        """Assemble the single multimodal turn: portrait blob, then instruction.

        Exactly two parts — the raw portrait as the first ``inline_data`` part
        and the profile-grounded instruction as the second. There is no
        intermediate text-generation step.
        """
        prompt = (
            "Create a hybrid animal portrait from this photograph, guided by the "
            "subject's behavioural profile below. Keep the subject's own face and "
            "blend in the suggested animal's features as a playful photo-booth "
            "transformation.\n\n"
            f"{profile.summary}\n\n"
            f"Suggested animal: {profile.suggested_animal}. "
            "Depict the subject as that animal in the wild."
        )
        return genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    inline_data=genai_types.Blob(data=portrait, mime_type="image/jpeg")
                ),
                genai_types.Part(text=prompt),
            ],
        )

    def _collect_final_parts(self, content: genai_types.Content, session_id: str) -> list:
        """Run the ADK agent once and return the final response's parts."""
        parts: list = []
        for event in self._runner.run(
            user_id=session_id, session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content and event.content.parts:
                parts.extend(event.content.parts)
        return parts

    @staticmethod
    def _looks_like_image(data: bytes) -> bool:
        """Cheap magic-byte sniff for JPEG/PNG at the extraction boundary."""
        return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")

    @staticmethod
    def _extract_image(parts: list) -> bytes:
        """Return the first ``inline_data.data`` image, or raise loudly.

        Text-only, empty, or non-image inline data are a failure to produce an
        image — never a silent success (typed-boundary rule, TECH.md).
        """
        for part in parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                if not Converter._looks_like_image(data):
                    raise ConverterError(
                        "converter model inline_data is not a sniffable JPEG/PNG image"
                    )
                return data
        raise ConverterError("converter model response contained no image data")

    def _run_llm(self, content: genai_types.Content, session_id: str) -> bytes:
        """Run the image agent and return the generated image bytes.

        Scriptable seam for offline tests (returns bytes, not text).
        """
        return self._extract_image(self._collect_final_parts(content, session_id))

    def _llm_bounded(self, content: genai_types.Content, session_id: str) -> bytes:
        """Run the LLM step with a hard timeout (The Bouncer's pattern).

        A hung Gemini request must not freeze the whole bot: past
        ``CONVERTER_GEMINI_TIMEOUT`` the orphaned worker is abandoned (daemon
        thread) and a ``TimeoutError`` is raised, which ``hybridize`` turns into
        a local-composer fallback.
        """
        timeout = float(
            os.getenv("CONVERTER_GEMINI_TIMEOUT", str(DEFAULT_GEMINI_TIMEOUT_SECONDS))
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

        worker = threading.Thread(target=_run, name="converter-gemini", daemon=True)
        worker.start()
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"converter gemini call exceeded {timeout:.0f}s") from None
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    @log_call(event="converter_hybridize")
    def hybridize(self, chat_id: int, portrait: bytes, profile: UserProfile) -> bytes:
        """Produce the hybrid image bytes for one chat.

        Gemini first (one multimodal call); on any failure/timeout/blocked key
        the local composer (if wired) produces a deterministic composite. With
        neither available — or a bad payload — raises ``ConverterError``.
        """
        if not isinstance(portrait, bytes) or not portrait:
            raise ConverterError(f"hybridize requires non-empty portrait bytes for chat {chat_id}")
        if not isinstance(profile, UserProfile):
            raise ConverterError(f"hybridize requires a UserProfile for chat {chat_id}")

        session_id = self._fresh_session(chat_id)
        content = self._prompt_content(portrait, profile)

        try:
            if self._has_gemini:
                try:
                    image = self._llm_bounded(content, session_id)
                    if not image:
                        raise ConverterError("converter model returned an empty image")
                    logger.info(
                        "event=hybrid_generated chat_id=%d bytes=%d", chat_id, len(image)
                    )
                    return image
                except Exception as exc:
                    logger.warning(
                        "event=converter_gemini_failed chat_id=%d error_type=%s error=%.120r fallback_local=%s",
                        chat_id,
                        type(exc).__name__,
                        exc,
                        self._local is not None,
                    )

            if self._local is None:
                raise ConverterError(
                    f"converter unavailable for chat {chat_id}: no image from Gemini "
                    "and no local composer wired"
                )

            try:
                image = self._local.compose(portrait, profile.suggested_animal)
            except Exception as exc:
                raise ConverterError(
                    f"local composer failed for chat {chat_id}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            logger.info(
                "event=converter_local_fallback chat_id=%d animal=%s bytes=%d",
                chat_id,
                profile.suggested_animal,
                len(image),
            )
            return image
        finally:
            # The conversion is one-shot: drop the session so neither history
            # nor the generated image bytes stay in memory for the chat's life.
            self._delete_session(chat_id)
