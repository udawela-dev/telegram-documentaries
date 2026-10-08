"""The Bouncer — the project's first Google ADK agent (ROADMAP Phase 2).

An ADK ``LlmAgent`` on **Gemini 3.1 Flash Lite** performs multimodal
classification of an uploaded photograph: does it contain a clearly
discernible human face or body?

* Human present → the gateway approves and the pipeline continues unchanged.
* No human (objects, animals, food, vehicles, landscapes, …) → the gateway
  replies with the cheeky ``BOUNCER_REJECTION`` and resets the chat's
  ephemeral state via ``reset_chat``.
* Any uncertainty or unparsable output → **reject safely**
  (``human_present: false``).

ADK wiring: ``LlmAgent`` + ``InMemorySessionService`` (one in-memory session
per chat_id — this IS the ephemeral conversation state) + a ``Runner``.
The model reads the API key from the environment (``GEMINI_API_KEY`` in
``.env``); nothing is ever hardcoded or logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict, ValidationError

from src.logging_utils import log_call

logger = logging.getLogger(__name__)

DEFAULT_BOUNCER_MODEL = "gemini-3.1-flash-lite"
# Highest-priority override: BOUNCER_MODEL=... env var (tests/admins).
BOUNCER_MODEL = os.getenv("BOUNCER_MODEL", DEFAULT_BOUNCER_MODEL)

# Locked copy (SPECS/2026-10-08-bouncer/requirements.md decision #3).
BOUNCER_REJECTION = (
    "Oi! 📸 No monsters, no sunsets, and definitely no last night's lasagna. "
    "I only do *humans* — a face, a torso, a faintly smug grin. "
    "Send me a picture of a person, mate."
)

# Locked copy (requirements.md decision #4) for download/classify failures.
BOUNCER_UNAVAILABLE_REPLY = (
    "Hang on — I couldn't get a good look at that photo. Mind sending it again?"
)

INSTRUCTION = (
    "You are The Bouncer, the front gate of a wildlife-documentary Telegram bot. "
    "A user has uploaded a photograph. Classify whether the image contains at "
    "least one clearly discernible HUMAN FACE or HUMAN BODY. "
    "Reject (human_present=false) anything without a human: objects, animals, "
    "food, vehicles, landscapes, scenery, drawings, cartoons, text, or "
    "distorted or hidden faces. If you are UNSURE at all, reject safely "
    "(human_present=false). "
    "Reply with ONLY strict JSON — no prose, no markdown fences — in exactly "
    'this shape: {"human_present": true|false, "reason": "one short sentence"}'
)


class BouncerDecision(BaseModel):
    """Typed verdict parsed from the agent's strict-JSON reply."""

    model_config = ConfigDict(extra="ignore", strict=True)

    human_present: bool
    reason: str = ""


def parse_decision(text: str) -> BouncerDecision:
    """Boundary: raw agent output → typed ``BouncerDecision``.

    Tolerates a markdown-fenced JSON block (some models wrap output), but any
    parse failure or missing field counts as **reject safe** — the gate never
    lets an ambiguous verdict through.
    """
    if not text or not text.strip():
        logger.warning("event=bouncer_empty_response treating_as_rejected")
        return BouncerDecision(human_present=False, reason="empty model response")

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        logger.warning(
            "event=bouncer_non_json_response treating_as_rejected raw=%.200s", text
        )
        return BouncerDecision(human_present=False, reason="model output was not strict JSON")

    try:
        data: Any = json.loads(match.group(0))
    except ValueError:
        logger.warning(
            "event=bouncer_invalid_json treating_as_rejected raw=%.200s", text
        )
        return BouncerDecision(human_present=False, reason="model output was not valid JSON")

    if not isinstance(data, dict):
        logger.warning("event=bouncer_non_object_json treating_as_rejected")
        return BouncerDecision(human_present=False, reason="model output was not a JSON object")

    try:
        return BouncerDecision.model_validate(data)
    except ValidationError as exc:
        logger.warning(
            "event=bouncer_invalid_decision treating_as_rejected errors=%s",
            "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:3]),
        )
        return BouncerDecision(human_present=False, reason="model output was missing required fields")


class Bouncer:
    """ADK vision gate: one ``LlmAgent`` + per-chat in-memory sessions."""

    APP_NAME = "telegram-documentaries"

    def __init__(self, api_key: str | None = None, model: str = BOUNCER_MODEL) -> None:
        # ADK's genai client resolves GEMINI_API_KEY from the environment; make
        # the one from our settings visible without ever hardcoding it.
        if api_key:
            os.environ.setdefault("GEMINI_API_KEY", api_key)
        self._model = model
        self._agent = LlmAgent(name="bouncer", model=model, instruction=INSTRUCTION)
        self._sessions = InMemorySessionService()
        self._runner = Runner(
            agent=self._agent,
            app_name=self.APP_NAME,
            session_service=self._sessions,
        )
        logger.info("event=bouncer_ready model=%s", model)

    def _session_id(self, chat_id: int) -> str:
        """One in-memory session = one chat_id (string form is right-sized)."""
        return str(chat_id)

    def _ensure_session(self, chat_id: int) -> str:
        session_id = self._session_id(chat_id)
        if (
            self._sessions.get_session_sync(
                app_name=self.APP_NAME, user_id=session_id, session_id=session_id
            )
            is None
        ):
            self._sessions.create_session_sync(
                app_name=self.APP_NAME, user_id=session_id, session_id=session_id
            )
        return session_id

    @log_call(event="bouncer_classify")
    def classify(
        self,
        image_bytes: bytes,
        chat_id: int,
        *,
        mime_type: str = "image/jpeg",
    ) -> BouncerDecision:
        """Gate one photograph for one chat. Never raises for a bad verdict."""
        session_id = self._ensure_session(chat_id)
        raw_text = self._llm_classify(image_bytes, session_id, mime_type)
        decision = parse_decision(raw_text)
        if not decision.human_present:
            logger.info(
                "event=bouncer_rejected chat_id=%d reason=%.160r", chat_id, decision.reason
            )
        return decision

    def _llm_classify(self, image_bytes: bytes, session_id: str, mime_type: str) -> str:
        """Run the ADK agent once; return the final response text.

        Split out so offline tests can script the model output without a
        network/genai dependency; production always uses the real agent.
        """
        content = genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    inline_data=genai_types.Blob(data=image_bytes, mime_type=mime_type)
                ),
                genai_types.Part(
                    text=(
                        "A user uploaded this photo. Is a clearly discernible "
                        "human face or body present?"
                    )
                ),
            ],
        )
        parts: list[str] = []
        for event in self._runner.run(
            user_id=session_id, session_id=session_id, new_message=content
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        parts.append(part.text)
        text = "".join(parts).strip()
        logger.info(
            "event=bouncer_raw_response bytes=%d response_chars=%d",
            len(image_bytes),
            len(text),
        )
        return text

    @log_call(event="bouncer_reset_chat")
    def reset_chat(self, chat_id: int) -> None:
        """Delete the ephemeral per-chat session (the pipeline's state reset).

        Idempotent and chat-scoped: deleting one chat's session can never
        touch another chat's.
        """
        session_id = self._session_id(chat_id)
        if (
            self._sessions.get_session_sync(
                app_name=self.APP_NAME, user_id=session_id, session_id=session_id
            )
            is not None
        ):
            self._sessions.delete_session_sync(
                app_name=self.APP_NAME, user_id=session_id, session_id=session_id
            )
            logger.info("event=bouncer_session_reset chat_id=%d", chat_id)