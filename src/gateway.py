"""Phase 1+2 gateway: long-polling loop + dispatcher (Bouncer gate since Phase 2).

Behaviour contract:
* every TEXT message gets the confirmation reply — no state, no branching;
* every PHOTO message passes through The Bouncer gate (Phase 2): approved →
  the confirmation reply is sent unchanged; rejected → cheeky rejection and
  the chat's ephemeral state is reset; download/classify failures → graceful
  reply, no reset, loop survives;
* ``offset`` advances after each update is processed OR attempted, so no
  update is ever reprocessed;
* a failing poll or a failing reply never kills the loop (fail loudly,
  log, keep going — SPECS/TECH.md).
"""

from __future__ import annotations

import logging
import threading

from src.bouncer import BOUNCER_REJECTION, BOUNCER_UNAVAILABLE_REPLY
from src.telegram_models import Message, TelegramAPIError, Update

logger = logging.getLogger(__name__)

REPLY_TEXT = "Hi Mate"


class Gateway:
    def __init__(
        self,
        client,
        *,
        bouncer=None,
        reply_text: str = REPLY_TEXT,
        poll_timeout: int = 30,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._bouncer = bouncer
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
        if update.message.photo:
            return self._handle_photo(update.message, chat_id, update.update_id)
        self._client.send_message(chat_id, self._reply_text)
        logger.info(
            "event=reply_sent update_id=%d chat_id=%s text=%r",
            update.update_id,
            chat_id,
            self._reply_text,
        )
        return True

    def _handle_photo(self, message: Message, chat_id: int, update_id: int) -> bool:
        """Phase 2 gate: photo uploads → The Bouncer.

        Approved → confirmation flow unchanged. Rejected → cheeky rejection +
        ephemeral chat state reset. Failure at any step → graceful reply, no
        reset, loop survives.
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
            return True

        logger.info(
            "event=photo_rejected update_id=%d chat_id=%s reason=%r",
            update_id,
            chat_id,
            decision.reason,
        )
        self._client.send_message(chat_id, BOUNCER_REJECTION)
        try:
            self._bouncer.reset_chat(chat_id)
        except Exception:
            logger.exception(
                "event=bouncer_session_reset_failed chat_id=%s", chat_id
            )
        return True

    def run(self, stop_event: threading.Event) -> None:
        """Poll forever until ``stop_event`` is set (Ctrl-C / SIGTERM)."""
        logger.info("event=polling_started reply_text=%r", self._reply_text)
        while not stop_event.is_set():
            self.poll_once()
            # No busy-wait: backoff between polls; the long poll itself blocks.
            stop_event.wait(self._backoff_seconds)
        logger.info("event=polling_stopped")