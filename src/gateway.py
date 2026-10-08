"""Phase 1 gateway: long-polling loop + dispatcher (plan task 6).

Behaviour contract:
* every message channel gets the SAME hardcoded reply — no state, no
  branching on content;
* ``offset`` advances after each update is processed OR attempted, so no
  update is ever reprocessed;
* a failing poll or a failing reply never kills the loop (fail loudly,
  log, keep going — SPECS/TECH.md).
"""

from __future__ import annotations

import logging
import threading

from src.telegram_models import TelegramAPIError, Update

logger = logging.getLogger(__name__)

REPLY_TEXT = "Hi Mate"


class Gateway:
    def __init__(
        self,
        client,
        *,
        reply_text: str = REPLY_TEXT,
        poll_timeout: int = 30,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._client = client
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
        self._client.send_message(chat_id, self._reply_text)
        logger.info(
            "event=reply_sent update_id=%d chat_id=%s text=%r",
            update.update_id,
            chat_id,
            self._reply_text,
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