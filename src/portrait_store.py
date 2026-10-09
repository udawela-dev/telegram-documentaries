"""In-memory PortraitStore (ROADMAP Phase 4).

The media counterpart of the shared interview state driver
(:mod:`src.interview_state`): the approved portrait's raw bytes, keyed by
``chat_id`` (int), held in memory only (TECH.md: in-memory, no disk).

Contract (SPECS/2026-10-08-converter/requirements.md):

* ``save`` / ``get`` / ``delete`` take an ``int`` ``chat_id``;
* ``get`` returns ``None`` for a chat with nothing saved yet;
* ``delete`` is idempotent and chat-scoped;
* a wrong-typed ``chat_id`` (including a ``bool``, which is an ``int`` subclass)
  or a non-``bytes`` payload → loud ``PortraitStoreError``;
* a corrupt value smuggled into the store is never returned silently.

Threading model: the app is a single-threaded long-polling loop, but the lock
protects the dict so individual reads/writes are always isolated.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class PortraitStoreError(RuntimeError):
    """Raised on a wrong-typed ``chat_id`` / payload or corrupt stored value."""


class PortraitStore:
    """In-memory, thread-safe, per-``chat_id`` store of raw portrait bytes."""

    def __init__(self) -> None:
        self._portraits: dict[int, bytes] = {}
        self._lock = threading.Lock()

    def save(self, chat_id: int, image_bytes: bytes) -> None:
        """Store the approved portrait's exact bytes for ``chat_id``.

        Raises ``PortraitStoreError`` for a wrong-typed ``chat_id`` or a
        non-``bytes`` payload (fail loud — never store a foreign value).
        """
        self._require_int_chat_id(chat_id)
        if not isinstance(image_bytes, bytes):
            raise PortraitStoreError(
                f"portrait for chat_id={chat_id} must be bytes, "
                f"got {type(image_bytes).__name__}"
            )
        with self._lock:
            self._portraits[chat_id] = image_bytes

    def get(self, chat_id: int) -> bytes | None:
        """Return ``chat_id``'s portrait bytes, or ``None`` if none is saved.

        A stored value that is not ``bytes`` is corrupt state and raises
        ``PortraitStoreError`` rather than being returned.
        """
        self._require_int_chat_id(chat_id)
        with self._lock:
            value = self._portraits.get(chat_id)
        if value is None:
            return None
        if not isinstance(value, bytes):
            raise PortraitStoreError(
                f"corrupt portrait for chat_id={chat_id}: "
                f"expected bytes, found {type(value).__name__}"
            )
        return value

    def delete(self, chat_id: int) -> None:
        """Purge one chat's portrait. Idempotent and chat-scoped."""
        self._require_int_chat_id(chat_id)
        with self._lock:
            self._portraits.pop(chat_id, None)

    @staticmethod
    def _require_int_chat_id(chat_id: int) -> None:
        # bool is a subclass of int; a bool chat_id is still wrong-typed input.
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise PortraitStoreError(
                f"chat_id must be int, got {type(chat_id).__name__}"
            )
