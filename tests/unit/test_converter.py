"""Unit tests for The Converter (Phase 4 — RED first).

Offline strategy mirrors ``test_bouncer.py``: the LLM step (``_run_llm``) is
scripted, so no network and no Gemini key are needed. The real behaviour under
test is the single-multimodal-call content assembly, the image-part extraction
boundary, the bounded timeout, the local-composer fallback, and the locked copy.
"""
import logging
import time

import pytest
from google.genai import types as genai_types

import src.converter as converter_module
from src.converter import (
    CONVERTER_MODEL,
    CONVERTER_REPLY_UNAVAILABLE,
    DEFAULT_CONVERTER_MODEL,
    Converter,
    ConverterError,
    resolve_model,
)
from src.interview_state import UserProfile

APP = "telegram-documentaries"
PORTRAIT = b"\xff\xd8\xff\xe0-portrait-bytes"


def _profile(animal: str = "the House Cat") -> UserProfile:
    return UserProfile(
        chat_id=42,
        summary=(
            "Personality profile:\n"
            "Habits: naps in the afternoon\n"
            "Quirks: talks to plants\n"
            "Routines: early coffee\n"
            "Preferences: quiet corners\n"
            f"Suggested animal: {animal}"
        ),
        suggested_animal=animal,
    )


# --- scriptable doubles ---------------------------------------------------------


class ScriptedConverter(Converter):
    """Converter whose LLM step returns preset image bytes (no network)."""

    def __init__(
        self, image_bytes: bytes = b"\xff\xd8\xffhybrid-image-bytes", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._scripted = image_bytes
        self.llm_calls: list[tuple[int, str]] = []

    def _run_llm(self, content, session_id: str) -> bytes:
        self.llm_calls.append((len(content.parts), session_id))
        return self._scripted


class FailingConverter(Converter):
    """Converter whose LLM step always fails — models a blocked/unreachable key."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.llm_calls = 0

    def _run_llm(self, content, session_id: str) -> bytes:
        self.llm_calls += 1
        raise RuntimeError("403 PERMISSION_DENIED: API key leaked")


class SpyConverter(Converter):
    """Records LLM invocations so tests can prove the LLM was skipped."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.llm_calls = 0

    def _run_llm(self, content, session_id: str) -> bytes:
        self.llm_calls += 1
        return b"should-not-be-used"


class FakeComposer:
    """Deterministic stand-in for :class:`LocalHybridComposer`."""

    def __init__(self, result: bytes = b"local-composite-bytes") -> None:
        self.result = result
        self.calls: list[tuple[bytes, str]] = []

    def compose(self, portrait_bytes: bytes, animal: str) -> bytes:
        self.calls.append((portrait_bytes, animal))
        return self.result


# --- content assembly: one multimodal Content, portrait first -------------------


def test_prompt_content_is_exactly_portrait_blob_then_instruction_text():
    converter = Converter(api_key="x")
    profile = _profile()

    content = converter._prompt_content(PORTRAIT, profile)

    assert isinstance(content, genai_types.Content)
    assert len(content.parts) == 2
    portrait_part, text_part = content.parts
    assert portrait_part.inline_data is not None
    assert portrait_part.inline_data.data == PORTRAIT
    assert portrait_part.inline_data.mime_type == "image/jpeg"
    assert portrait_part.text is None
    assert text_part.inline_data is None
    assert text_part.text is not None


def test_prompt_text_embeds_profile_summary_and_suggested_animal():
    converter = Converter(api_key="x")
    profile = _profile("the Lone Wolf")

    text = converter._prompt_content(PORTRAIT, profile).parts[1].text

    assert profile.summary in text
    assert profile.suggested_animal in text
    assert "Suggested animal:" in text


def test_agent_requests_image_modality_via_generate_content_config():
    converter = Converter(api_key="x")

    config = converter._agent.generate_content_config
    assert config is not None
    assert config.response_modalities == ["IMAGE"]


# --- _run_llm seam: scripted bytes flow straight through ------------------------


def test_hybridize_returns_scripted_llm_image_in_a_single_call(caplog):
    converter = ScriptedConverter(api_key="x")

    with caplog.at_level(logging.INFO):
        result = converter.hybridize(4242, PORTRAIT, _profile())

    assert result == b"\xff\xd8\xffhybrid-image-bytes"
    assert converter.llm_calls == [(2, "4242")]  # exactly one call, two-part Content
    assert any("event=hybrid_generated" in r.message for r in caplog.records)


def test_every_hybridize_starts_from_a_fresh_clean_session():
    """Conversions are one-shot single multimodal turns: a reused ADK session
    would leak the previous portrait/prompt/image as history and keep those
    bytes in memory forever, so each hybridize must begin (and end) clean."""
    converter = ScriptedConverter(api_key="x")

    converter.hybridize(111, PORTRAIT, _profile())
    assert converter._sessions.list_sessions_sync(app_name=APP, user_id="111").sessions == []

    # A second cycle in the SAME chat starts with zero history to leak.
    converter.hybridize(111, PORTRAIT, _profile())
    assert converter._sessions.list_sessions_sync(app_name=APP, user_id="111").sessions == []

    # A different chat is fully independent.
    converter.hybridize(222, PORTRAIT, _profile())
    assert converter._sessions.list_sessions_sync(app_name=APP, user_id="222").sessions == []
    assert converter._sessions.list_sessions_sync(app_name=APP, user_id="111").sessions == []


# --- image-part extraction boundary ---------------------------------------------


def _converter_returning_parts(parts: list) -> Converter:
    class PartsConverter(Converter):
        def _collect_final_parts(self, content, session_id: str) -> list:
            return parts

    return PartsConverter(api_key="x")


def test_run_llm_extracts_the_inline_data_image_from_final_response():
    parts = [
        genai_types.Part(text="Here is your hybrid portrait"),
        genai_types.Part(
            inline_data=genai_types.Blob(data=b"\xff\xd8\xffJPEGDATA", mime_type="image/jpeg")
        ),
    ]
    converter = _converter_returning_parts(parts)
    content = genai_types.Content(parts=[genai_types.Part(text="make me a hybrid")])

    assert converter._run_llm(content, "1") == b"\xff\xd8\xffJPEGDATA"


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param([genai_types.Part(text="I cannot create images")], id="text-only"),
        pytest.param([], id="no-parts"),
        pytest.param(
            [genai_types.Part(inline_data=genai_types.Blob(mime_type="image/jpeg"))],
            id="no-inline-data",
        ),
    ],
)
def test_run_llm_raises_loudly_when_the_response_has_no_image(parts):
    converter = _converter_returning_parts(parts)
    content = genai_types.Content(parts=[genai_types.Part(text="make me a hybrid")])

    with pytest.raises(ConverterError):
        converter._run_llm(content, "1")


def test_hybridize_with_no_composer_and_garbage_llm_response_raises():
    """Never a fake 'image': a text-only response with no fallback must raise."""
    converter = _converter_returning_parts([genai_types.Part(text="no image for you")])

    with pytest.raises(ConverterError):
        converter.hybridize(5, PORTRAIT, _profile())


class _FakeEvent:
    """Minimal ADK event double for the response-extraction seam."""

    def __init__(self, *, final: bool, parts: list | None) -> None:
        self._final = final
        self.content = genai_types.Content(parts=parts) if parts is not None else None

    def is_final_response(self) -> bool:
        return self._final


class _FakeRunner:
    def __init__(self, events: list) -> None:
        self._events = events

    def run(self, user_id, session_id, new_message):
        return iter(self._events)


def test_run_llm_uses_only_the_final_response_parts():
    """Interim events are ignored; the image comes from the final response."""
    image_part = genai_types.Part(
        inline_data=genai_types.Blob(data=b"\xff\xd8\xffFINAL-IMAGE", mime_type="image/jpeg")
    )
    events = [
        _FakeEvent(final=False, parts=[image_part]),  # interim image must be ignored
        _FakeEvent(final=True, parts=[genai_types.Part(text="here it is"), image_part]),
        _FakeEvent(final=True, parts=None),  # empty final event must be safe
    ]
    converter = Converter(api_key="x")
    converter._runner = _FakeRunner(events)

    result = converter._run_llm(converter._prompt_content(PORTRAIT, _profile()), "1")

    assert result == b"\xff\xd8\xffFINAL-IMAGE"


def test_run_llm_raises_when_only_a_non_final_event_carries_an_image():
    image_part = genai_types.Part(
        inline_data=genai_types.Blob(data=b"INTERIM-IMAGE", mime_type="image/jpeg")
    )
    events = [
        _FakeEvent(final=False, parts=[image_part]),
        _FakeEvent(final=True, parts=[genai_types.Part(text="no image")]),
    ]
    converter = Converter(api_key="x")
    converter._runner = _FakeRunner(events)

    with pytest.raises(ConverterError):
        converter._run_llm(converter._prompt_content(PORTRAIT, _profile()), "1")


# --- local-composer fallback ----------------------------------------------------


def test_llm_failure_uses_local_composer_with_portrait_and_animal(caplog):
    composer = FakeComposer()
    converter = FailingConverter(api_key="x", local_composer=composer)
    profile = _profile("the Lone Wolf")

    with caplog.at_level(logging.INFO):
        result = converter.hybridize(1, PORTRAIT, profile)

    assert result == b"local-composite-bytes"
    assert composer.calls == [(PORTRAIT, "the Lone Wolf")]
    assert any("event=converter_local_fallback" in r.message for r in caplog.records)


def test_hybridize_with_llm_failure_and_no_composer_raises():
    converter = FailingConverter(api_key="x")  # no local composer wired

    with pytest.raises(ConverterError):
        converter.hybridize(5, PORTRAIT, _profile())


def test_no_gemini_key_goes_straight_to_local_composer(monkeypatch):
    """No key in view + composer wired → the keyless LLM round-trip is skipped."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    composer = FakeComposer()
    converter = SpyConverter(api_key=None, local_composer=composer)

    result = converter.hybridize(7, PORTRAIT, _profile("the Sea Otter"))

    assert result == b"local-composite-bytes"
    assert converter.llm_calls == 0
    assert composer.calls == [(PORTRAIT, "the Sea Otter")]


def test_timeout_falls_back_to_local_composer(monkeypatch):
    """A hung Gemini call must not freeze the bot past the bounded timeout."""
    monkeypatch.setenv("CONVERTER_GEMINI_TIMEOUT", "0.2")

    class HangingConverter(Converter):
        def _run_llm(self, content, session_id: str) -> bytes:
            time.sleep(1)
            return b"late"

    composer = FakeComposer()
    converter = HangingConverter(api_key="x", local_composer=composer)
    started = time.monotonic()

    result = converter.hybridize(9, PORTRAIT, _profile())

    elapsed = time.monotonic() - started
    assert result == b"local-composite-bytes"
    assert elapsed < 0.8  # bounded by the 0.2s timeout, not the 1s sleep


# --- construction / constants ---------------------------------------------------


def test_constructor_honours_explicit_model():
    converter = Converter(api_key="x", model="gemini-custom-image")

    assert converter.model == "gemini-custom-image"


def test_default_model_is_gemini_flash_image():
    assert DEFAULT_CONVERTER_MODEL == "gemini-3.1-flash-image"
    assert CONVERTER_MODEL == "gemini-3.1-flash-image"


def test_resolve_model_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("CONVERTER_MODEL", raising=False)

    assert resolve_model() == "gemini-3.1-flash-image"


def test_resolve_model_honours_env_override(monkeypatch):
    monkeypatch.setenv("CONVERTER_MODEL", "gemini-9.9-test-override")

    assert resolve_model() == "gemini-9.9-test-override"
    assert converter_module.resolve_model() == "gemini-9.9-test-override"


def test_unavailable_reply_copy_is_locked():
    assert CONVERTER_REPLY_UNAVAILABLE == (
        "Hang on — I couldn't quite finish your portrait. Give that another go?"
    )
