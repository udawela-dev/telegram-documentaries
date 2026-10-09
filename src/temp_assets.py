"""Per-chat temporary asset registry.

Tracks on-disk temporary files staged for a specific chat so a reset can
sweep orphaned media (e.g. Narrator .wav/.ogg and Converter temp media).
The registry is the sweep-net that ``/restart`` uses; finally-cleanup
remains the primary path. Purge failures are logged loud, never fatal.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class TempAssetsError(RuntimeError):
    """Raised on a wrong-typed ``chat_id`` input."""


class TempAssets:
    """Thread-safe, per-``chat_id`` registry of staged temporary paths."""

    def __init__(self) -> None:
        self._assets: dict[int, set[Path]] = {}
        self._lock = threading.Lock()

    def track(self, chat_id: int, path: str | Path) -> None:
        """Register ``path`` as a staged temp file for ``chat_id``."""
        self._require_int_chat_id(chat_id)
        p = Path(path)
        with self._lock:
            self._assets.setdefault(chat_id, set()).add(p)

    def purge(self, chat_id: int) -> None:
        """Unlink every tracked path for ``chat_id`` and remove entries.

        Each unlink failure is logged loud but never raised; the loop must
        never die because of a temp-file cleanup failure.
        """
        self._require_int_chat_id(chat_id)
        with self._lock:
            paths = self._assets.pop(chat_id, set())
        for p in paths:
            try:
                p.unlink()
                logger.info("event=temp_asset_unlinked chat_id=%d path=%s", chat_id, p)
            except FileNotFoundError:
                # The file was already gone (the stage's own finally-cleanup or a
                # previous sweep); absent is the desired state — still logged so
                # the sweep is visible, never treated as a failure.
                logger.info(
                    "event=temp_asset_unlinked chat_id=%d path=%s already_missing=True",
                    chat_id,
                    p,
                )
            except Exception:
                logger.exception("event=temp_asset_purge_failed chat_id=%d path=%s", chat_id, p)

    @staticmethod
    def _require_int_chat_id(chat_id: int) -> None:
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise TempAssetsError(
                f"chat_id must be int, got {type(chat_id).__name__}"
            )
