"""Unit tests for the key-free local photo-booth composer (Phase 4 — RED first).

The composer is the deterministic OpenCV fallback used while the real
``gemini-3.1-flash-image`` path is key-blocked. It locates the largest face and
draws an animal overlay; identical input must yield byte-identical output.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.local_composite import LocalHybridComposer

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PERSON_JPEG = (FIXTURES / "person.jpg").read_bytes()
NON_HUMAN_JPEG = (FIXTURES / "non_human.jpg").read_bytes()


@pytest.fixture(scope="module")
def composer() -> LocalHybridComposer:
    return LocalHybridComposer()


def _decodes_to_image(payload: bytes) -> bool:
    return cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR) is not None


# --- happy path ----------------------------------------------------------------


def test_compose_returns_decodable_non_trivial_jpeg_with_a_face(composer):
    result = composer.compose(PERSON_JPEG, "the House Cat")

    assert isinstance(result, bytes)
    assert _decodes_to_image(result)
    assert len(result) > 1000  # a real rendered image, not a stub


def test_compose_still_returns_an_image_when_no_face_is_present(composer):
    """No detected face → the locked default overlay is placed centrally."""
    result = composer.compose(NON_HUMAN_JPEG, "the House Cat")

    assert _decodes_to_image(result)
    assert len(result) > 1000


def test_compose_supports_multiple_archetypes(composer):
    for animal in ("the House Cat", "the Lone Wolf", "the Mountain Goat", "the Night Owl", "the Sea Otter"):
        result = composer.compose(PERSON_JPEG, animal)
        assert _decodes_to_image(result), animal


def test_different_archetypes_produce_different_images(composer):
    cat = composer.compose(PERSON_JPEG, "the House Cat")
    wolf = composer.compose(PERSON_JPEG, "the Lone Wolf")

    assert cat != wolf


def test_unknown_animal_falls_back_to_the_locked_default_style(composer):
    """Any unrecognised suggested animal uses the cat-like default archetype."""
    unknown = composer.compose(PERSON_JPEG, "the Velociraptor")
    default = composer.compose(PERSON_JPEG, "the House Cat")

    assert unknown == default


# --- determinism ---------------------------------------------------------------


def test_compose_is_deterministic_for_the_same_input(composer):
    first = composer.compose(PERSON_JPEG, "the Lone Wolf")
    second = composer.compose(PERSON_JPEG, "the Lone Wolf")

    assert first == second


# --- boundary: undecodable input ------------------------------------------------


def test_undecodable_bytes_raise_loudly(composer):
    with pytest.raises(ValueError, match="decode"):
        composer.compose(b"this is definitely not an image", "the House Cat")


def test_empty_bytes_raise_loudly(composer):
    with pytest.raises(ValueError, match="decode"):
        composer.compose(b"", "the House Cat")
