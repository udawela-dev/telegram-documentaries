"""Live Gemini verification for The Converter (guarded; skipped offline).

Proves the real ``gemini-3.1-flash-image`` path — one multimodal call with the
raw portrait + profile text, returning a generated image — when a healthy key
exists. In this repository's environment the Gemini key is blocked (403), so
this test stays SKIPPED unless ``RUN_LIVE_GEMINI=1`` is set; that skip is
expected and correct.

Run with:
    RUN_LIVE_GEMINI=1 python3 -m pytest tests/integration/test_live_converter.py -v
"""
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.config import load_settings
from src.converter import CONVERTER_MODEL, Converter
from src.interview_state import UserProfile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="set RUN_LIVE_GEMINI=1 to run the live Gemini image tests",
)


@pytest.fixture(scope="module")
def converter() -> Converter:
    settings = load_settings()
    if not settings.gemini_api_key:
        pytest.fail("GEMINI_API_KEY missing from .env — cannot run live Converter tests")
    return Converter(api_key=settings.gemini_api_key, model=CONVERTER_MODEL)


def _profile() -> UserProfile:
    return UserProfile(
        chat_id=99002,
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


def test_person_photo_is_converted_to_a_real_hybrid_image(converter: Converter) -> None:
    portrait = (FIXTURES / "person.jpg").read_bytes()

    image = converter.hybridize(99002, portrait, _profile())

    assert isinstance(image, bytes)
    assert len(image) > 1000  # a real generated image, not a stub
    decoded = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
