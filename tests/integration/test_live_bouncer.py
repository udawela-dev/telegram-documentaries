"""Live Gemini verification for The Bouncer (guarded; skipped offline).

These are the two acceptance tests from SPECS/2026-10-08-bouncer/validation.md
and the user's brief:
    1. a NON-HUMAN photo (mountain landscape) is REJECTED;
    2. a clear PERSON photo is APPROVED (continues the confirmation flow).

Requires GEMINI_API_KEY in .env → run with:
    RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_bouncer.py -v
"""
import os
from pathlib import Path

import pytest

from src.bouncer import Bouncer, BOUNCER_MODEL
from src.config import load_settings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="set RUN_LIVE_GEMINI=1 to run the live Gemini vision tests",
)


@pytest.fixture(scope="module")
def bouncer() -> Bouncer:
    settings = load_settings()
    if not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY missing from .env — cannot run live Bouncer tests")
    return Bouncer(api_key=settings.gemini_api_key, model=BOUNCER_MODEL)


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        pytest.fail(f"fixture missing: {path}")
    return path.read_bytes()


def test_non_human_photo_is_rejected(bouncer: Bouncer) -> None:
    decision = bouncer.classify(_fixture_bytes("non_human.jpg"), chat_id=99001)

    assert decision.human_present is False, decision.reason


def test_person_photo_is_approved(bouncer: Bouncer) -> None:
    decision = bouncer.classify(_fixture_bytes("person.jpg"), chat_id=99002)

    assert decision.human_present is True, decision.reason