"""Key-free local script writer (ROADMAP Phase 5).

While the real ``gemini-3.1-flash-lite`` narration path is blocked (403) or
otherwise unreachable, The Scripter falls back to this deterministic, key-free
writer so the pipeline still produces a real British-wildlife-documentary
paragraph in Telegram today. A healthy key later swaps in the real paragraph
with no code change.

The output is a single 60–90 word paragraph grounded in the profile's
``suggested_animal`` and a locked narrative template. Identical input yields
identical output; the Scripter's :func:`validate_script` still gates it, so a
shape violation here can never be silently accepted.
"""

from __future__ import annotations

import logging

from src.interview_state import UserProfile

logger = logging.getLogger(__name__)

# Locked narrative template: one paragraph, documentary voice, with the
# suggested animal dropped in. Kept at 82–83 words for every canonical animal
# (the animal contributes 2–3 words) so it always sits inside the 60–90 budget.
TEMPLATE = (
    "Behold, in the quiet corners of everyday life, a remarkable specimen: "
    "{animal}. Watch closely as it moves through the familiar rituals of its "
    "habitat, guided by habit and gentle instinct. Note the telltale quirks, "
    "the small routines performed with solemn devotion, and the preferences "
    "it guards like treasure. There, did you catch it? A flash of personality, "
    "unmistakable. This creature thrives not in the wilderness, but in the "
    "warm clutter of ordinary life, and is all the more extraordinary for it."
)


class LocalScriptWriter:
    """Deterministic, key-free 60–90 word documentary paragraph writer."""

    def __init__(self) -> None:
        logger.info("event=local_script_ready")

    def write(self, profile: UserProfile) -> str:
        """Return a deterministic single-paragraph narration for ``profile``."""
        animal = profile.suggested_animal
        script = TEMPLATE.format(animal=animal)
        logger.info(
            "event=local_script_done animal=%s words=%d", animal, len(script.split())
        )
        return script
