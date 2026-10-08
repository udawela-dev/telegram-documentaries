"""Phase 1+2+3 gateway: long-polling loop + dispatcher (Bouncer gate, Interviewer flow).

Behaviour contract:
* every TEXT message gets the confirmation reply — no state, no branching —
  when no interviewer is wired (backward compatible default);
* with an interviewer, text routes on the chat's interview phase: ``idle`` →
  "Hi Mate" (unchanged), ``interviewing`` → store the answer and send exactly
  the one next question, ``complete`` → resend the stored profile summary;
* every PHOTO message passes through The Bouncer gate (Phase 2): approved →
  the confirmation reply is sent unchanged, then a second message announces
  the verdict ("Human detected ✓"), then the interview starts (Q1); rejected →
  cheeky rejection, then a second message ("Non-human detected"), and both the
  chat's Bouncer session and interview state are reset; download/classify
  failures → graceful reply, no reset, loop survives;
* ``/start`` and ``/restart`` (with an interviewer wired) reset both stages and
  send a confirmation reply; without an interviewer every text, commands
  included, gets the confirmation reply (Phase-1 compatibility);
* ``offset`` advances after each update is processed OR attempted, so no
  update is ever reprocessed;
* a failing poll or a failing reply never kills the loop (fail loudly,
  log, keep going — SPECS/TECH.md).
* The dispatch loop is single-threaded: one update at a time. Interview
  state transitions (get → mutate → save on the shared driver) are
  therefore effectively atomic in production (see
  :mod:`src.interview_state`).
"""

from __future__ import annotations

import logging
import threading

from src.bouncer import (
    BOUNCER_REJECTION,
    BOUNCER_UNAVAILABLE_REPLY,
    HUMAN_VERDICT_REPLY,
    NON_HUMAN_VERDICT_REPLY,
)
from src.interview_state import InterviewPhase
from src.interviewer import INTERVIEWER_REPLY_RESET, INTERVIEWER_REPLY_UNAVAILABLE
from src.telegram_models import Message, TelegramAPIError, Update

logger = logging.getLogger(__name__)

REPLY_TEXT = "Hi Mate"


class Gateway:
    def __init__(
        self,
        client,
        *,
        bouncer=None,
        interviewer=None,
        reply_text: str = REPLY_TEXT,
        poll_timeout: int = 30,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._bouncer = bouncer
        # When None, text always says "Hi Mate" (Phase-1 backward compatibility).
        self._interviewer = interviewer
        self._reply_text = reply_text
        self._poll_timeout = poll_timeout
        self._backoff_seconds = backoff_seconds
        self.offset: int = 0  # next offset = highest processed update_id + 1

    def poll_once(self) -> int:
        """Poll, dispatch, advance the offset. Returns how many replies were sent.

        Poll failures are logged and return 0; individual update failures are
        logged and skipped — the batch is still consumed so nothing loops forever.
        """
        try:
            updates = self._client.get_updates(
                offset=self.offset or None,  # 0 means "no offset yet"
                timeout=self._poll_timeout,
            )
        except TelegramAPIError:
            logger.exception("event=poll_failed offset=%s", self.offset or None)
            return 0
        except Exception:
            logger.exception("event=poll_unexpected_failure offset=%s", self.offset or None)
            return 0

        replied = 0
        for update in updates:
            try:
                if self._dispatch(update):
                    replied += 1
            except TelegramAPIError:
                chat_id = update.message.chat.id if update.message else "?"
                logger.exception(
                    "event=update_reply_failed update_id=%s chat_id=%s",
                    getattr(update, "update_id", "?"),
                    chat_id,
                )
            except Exception:
                logger.exception(
                    "event=update_unexpected_failure update_id=%s",
                    getattr(update, "update_id", "?"),
                )
            # Offset advances after processing or *attempting* (also when an
            # entry is malformed), so nothing is ever retried forever and one
            # bad entry can never kill the loop.
            try:
                self.offset = max(self.offset, update.update_id)
            except Exception:
                logger.exception("event=update_offset_failed update=%r", update)
        self.offset += 1 if updates else 0
        return replied

    def _dispatch(self, update: Update) -> bool:
        """Reply to one update. Returns True when a reply was sent."""
        if update.message is None:
            logger.info(
                "event=skip_update_no_message update_id=%d (edited/channel/other)",
                update.update_id,
            )
            return False
        chat_id = update.message.chat.id
        # Route on presence of the field, not truthiness: a malformed message
        # with a photo=[] list must hit the gate, never the text branch.
        if update.message.photo is not None:
            return self._handle_photo(update.message, chat_id, update.update_id)
        return self._handle_text(update.message, chat_id, update.update_id)

    def _handle_text(self, message: Message, chat_id: int, update_id: int) -> bool:
        """Route a text message by interview phase (Phase 3).

        Without an interviewer this is exactly Phase 1: every text — including
        ``/start`` / ``/restart`` — gets the confirmation reply. With an
        interviewer, commands reset both stages, otherwise the reply depends on
        the chat's interview phase.
        """
        if self._interviewer is None:
            return self._reply_confirmation(chat_id, update_id)

        text = message.text or ""
        if text.startswith("/start") or text.startswith("/restart"):
            return self._handle_reset_command(chat_id, update_id)

        try:
            state = self._interviewer.state(chat_id)
        except Exception:
            logger.exception(
                "event=interview_state_failed chat_id=%s update_id=%d",
                chat_id,
                update_id,
            )
            return self._reply_confirmation(chat_id, update_id)

        if state.phase is InterviewPhase.INTERVIEWING:
            return self._handle_interview_answer(chat_id, message.text, update_id)
        if state.phase is InterviewPhase.COMPLETE:
            if state.profile is None:
                # An inconsistent state must never quietly degrade to "Hi Mate"
                # without a trace (TECH.md: fail loud, no un-logged fallbacks).
                logger.error(
                    "event=interview_complete_missing_profile chat_id=%s update_id=%d",
                    chat_id,
                    update_id,
                )
                return self._reply_confirmation(chat_id, update_id)
            self._client.send_message(chat_id, state.profile.summary)
            logger.info(
                "event=interview_profile_resent chat_id=%s update_id=%d",
                chat_id,
                update_id,
            )
            return True
        # IDLE (or any unknown phase): the Phase-1 confirmation reply, unchanged.
        return self._reply_confirmation(chat_id, update_id)

    def _reply_confirmation(self, chat_id: int, update_id: int) -> bool:
        self._client.send_message(chat_id, self._reply_text)
        logger.info(
            "event=reply_sent update_id=%d chat_id=%s text=%r",
            update_id,
            chat_id,
            self._reply_text,
        )
        return True

    def _handle_interview_answer(self, chat_id: int, text: str | None, update_id: int) -> bool:
        """Store one answer and deliver exactly the one next question (or profile)."""
        if text is None:
            logger.info(
                "event=interview_answer_missing_text chat_id=%s update_id=%d",
                chat_id,
                update_id,
            )
            return self._reply_confirmation(chat_id, update_id)
        try:
            reply = self._interviewer.answer(chat_id, text)
        except Exception:
            logger.exception(
                "event=interview_answer_failed chat_id=%s update_id=%d",
                chat_id,
                update_id,
            )
            self._client.send_message(chat_id, INTERVIEWER_REPLY_UNAVAILABLE)
            return True
        for outbound in reply.messages:
            self._client.send_message(chat_id, outbound)
        return True

    def _handle_reset_command(self, chat_id: int, update_id: int) -> bool:
        """``/start`` or ``/restart``: purge Bouncer session + Interviewer state.

        Never fails the loop: each reset is attempted independently and any
        failure is logged loudly before the confirmation reply is sent.
        """
        if self._bouncer is not None:
            try:
                self._bouncer.reset_chat(chat_id)
            except Exception:
                logger.exception("event=bouncer_session_reset_failed chat_id=%s", chat_id)
        if self._interviewer is not None:
            try:
                self._interviewer.reset(chat_id)
            except Exception:
                logger.exception("event=interview_reset_failed chat_id=%s", chat_id)
        self._client.send_message(chat_id, INTERVIEWER_REPLY_RESET)
        logger.info("event=reset_command chat_id=%s update_id=%d", chat_id, update_id)
        return True

    def _handle_photo(self, message: Message, chat_id: int, update_id: int) -> bool:
        """Photo uploads → The Bouncer; an approved photo starts the interview.

        Approved → confirmation flow unchanged, then Q1. Rejected → cheeky
        rejection + Bouncer session and interview state reset. Failure at any
        step → graceful reply, no reset, loop survives.
        """
        if self._bouncer is None:
            logger.error(
                "event=photo_no_bouncer chat_id=%s update_id=%d", chat_id, update_id
            )
            self._client.send_message(chat_id, BOUNCER_UNAVAILABLE_REPLY)
            return True
        if not message.photo:
            logger.error(
                "event=photo_empty chat_id=%s update_id=%d", chat_id, update_id
            )
            self._client.send_message(chat_id, BOUNCER_UNAVAILABLE_REPLY)
            return True
        try:
            largest = max(message.photo, key=lambda p: p.width * p.height)
            file_info = self._client.get_file(largest.file_id)
            if file_info.file_path is None:
                raise TelegramAPIError("getFile returned no file_path")
            image_bytes = self._client.download_file(file_info.file_path)
        except TelegramAPIError:
            logger.exception(
                "event=photo_download_failed chat_id=%s update_id=%d", chat_id, update_id
            )
            self._client.send_message(chat_id, BOUNCER_UNAVAILABLE_REPLY)
            return True
        except Exception:
            logger.exception(
                "event=photo_download_failed chat_id=%s update_id=%d", chat_id, update_id
            )
            self._client.send_message(chat_id, BOUNCER_UNAVAILABLE_REPLY)
            return True

        try:
            decision = self._bouncer.classify(image_bytes, chat_id)
        except Exception:
            logger.exception(
                "event=photo_classify_failed chat_id=%s update_id=%d", chat_id, update_id
            )
            self._client.send_message(chat_id, BOUNCER_UNAVAILABLE_REPLY)
            return True

        if decision.human_present:
            logger.info(
                "event=photo_approved update_id=%d chat_id=%s reason=%r",
                update_id,
                chat_id,
                decision.reason,
            )
            self._client.send_message(chat_id, self._reply_text)
            self._client.send_message(chat_id, HUMAN_VERDICT_REPLY)
            logger.info(
                "event=photo_verdict_sent update_id=%d chat_id=%s verdict=human",
                update_id,
                chat_id,
            )
            # Approved photo = the interview's entry point: Q1 after the verdict.
            if self._interviewer is not None:
                try:
                    first_question = self._interviewer.start(chat_id)
                    self._client.send_message(chat_id, first_question)
                except Exception:
                    logger.exception(
                        "event=interview_start_failed chat_id=%s update_id=%d",
                        chat_id,
                        update_id,
                    )
                    self._client.send_message(chat_id, INTERVIEWER_REPLY_UNAVAILABLE)
            return True

        logger.info(
            "event=photo_rejected update_id=%d chat_id=%s reason=%r",
            update_id,
            chat_id,
            decision.reason,
        )
        self._client.send_message(chat_id, BOUNCER_REJECTION)
        self._client.send_message(chat_id, NON_HUMAN_VERDICT_REPLY)
        logger.info(
            "event=photo_verdict_sent update_id=%d chat_id=%s verdict=non_human",
            update_id,
            chat_id,
        )
        try:
            self._bouncer.reset_chat(chat_id)
        except Exception:
            logger.exception(
                "event=bouncer_session_reset_failed chat_id=%s", chat_id
            )
        # A rejected photo purges the interview too (mirrors the Bouncer reset).
        if self._interviewer is not None:
            try:
                self._interviewer.reset(chat_id)
            except Exception:
                logger.exception("event=interview_reset_failed chat_id=%s", chat_id)
        return True

    def run(self, stop_event: threading.Event) -> None:
        """Poll forever until ``stop_event`` is set (Ctrl-C / SIGTERM)."""
        logger.info("event=polling_started reply_text=%r", self._reply_text)
        while not stop_event.is_set():
            self.poll_once()
            # No busy-wait: backoff between polls; the long poll itself blocks.
            stop_event.wait(self._backoff_seconds)
        logger.info("event=polling_stopped")
