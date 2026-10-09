"""Live Gemini verification for The Scripter (guarded; skipped offline).

Proves the real ``gemini-3.1-flash-lite`` path — one text turn on the completed
profile, returning a genuine British-wildlife-documentary paragraph — when a
healthy key exists. In this repository's environment the Gemini key is blocked
(403), so this test stays SKIPPED unless ``RUN_LIVE_GEMINI=1`` is set; that skip
is expected and correct.

Built **without** a local writer, so a valid paragraph can only come from the
real model (a local fallback would be off-contract for this proof).

Run with:
    RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_scripter.py -v
"""
import os

import pytest

from src.config import load_settings
from src.interview_state import InterviewStateStore, UserProfile
from src.scripter import SCRIPTER_MODEL, Scripter, validate_script

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="set RUN_LIVE_GEMINI=1 to run the live Gemini narration tests",
)


@pytest.fixture(scope="module")
def live_scripter() -> tuple[Scripter, InterviewStateStore]:
    settings = load_settings()
    if not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY missing from .env — cannot run live Scripter tests")
    store = InterviewStateStore()
    scripter = Scripter(
        api_key=settings.gemini_api_key,
        model=SCRIPTER_MODEL,
        store=store,
        local_writer=None,  # no fallback: this must be the real model's paragraph
    )
    return scripter, store


def _profile() -> UserProfile:
    return UserProfile(
        chat_id=99003,
        summary=(
            "Personality profile:\n"
            "Habits: early riser\n"
            "Quirks: hums while thinking\n"
            "Routines: long evening walks\n"
            "Preferences: quiet corners\n"
            "Suggested animal: the House Cat"
        ),
        suggested_animal="the House Cat",
    )


def test_real_flash_lite_returns_a_valid_grounded_paragraph(live_scripter) -> None:
    scripter, store = live_scripter
    profile = _profile()

    script = scripter.write_script(99003, profile)

    assert isinstance(script, str)
    assert "\n\n" not in script.strip()  # exactly one paragraph
    words = script.split()
    assert 60 <= len(words) <= 90
    assert profile.suggested_animal in script  # grounded in the dossier
    assert validate_script(script) == script  # passes the single shape gate
    assert store.get(99003).script == script  # stored for Phase 6
