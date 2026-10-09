"""Unit tests for The Scripter (Phase 5 — RED first).

Offline strategy mirrors ``test_converter.py``: the LLM step (``_run_llm``) is
scripted, so no network and no Gemini key are needed. The real behaviour under
test is the deterministic shape validator (``validate_script``), the
profile-grounded prompt assembly, the single text seam, the fresh-then-reaped
per-call ADK sessions, the bounded timeout, the key-free ``LocalScriptWriter``
fallback, the persistence onto the shared state driver, and the locked copy.
"""
import logging
import time

import pytest
from google.genai import types as genai_types

import src.scripter as scripter_module
from src.interview_state import (
    SCHEMA_VERSION,
    InterviewPhase,
    InterviewState,
    InterviewStateError,
    InterviewStateStore,
    UserProfile,
    utc_now_iso,
)
from src.scripter import (
    DEFAULT_SCRIPTER_MODEL,
    SCRIPTER_MAX_WORDS,
    SCRIPTER_MIN_WORDS,
    SCRIPTER_MODEL,
    SCRIPTER_REPLY_UNAVAILABLE,
    Scripter,
    ScripterError,
    resolve_model,
    validate_script,
)

APP = "telegram-documentaries"


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


def _paragraph(word_count: int) -> str:
    """A clean single-line paragraph with exactly ``word_count`` words."""
    return " ".join(f"word{i}" for i in range(word_count))


def _save_state(store: InterviewStateStore, chat_id: int, profile: UserProfile) -> None:
    store.save(
        chat_id,
        InterviewState(
            chat_id=chat_id,
            phase=InterviewPhase.COMPLETE,
            question_index=7,
            answers=[("q1", "a1")],
            profile=profile,
            updated_at=utc_now_iso(),
        ),
    )


def _sessions(scripter: Scripter, chat_id: int) -> list:
    """The chat's live ADK sessions — the reap contract asserts this is empty."""
    return scripter._sessions.list_sessions_sync(
        app_name=APP, user_id=str(chat_id)
    ).sessions


# --- scriptable doubles ---------------------------------------------------------


class ScriptedScripter(Scripter):
    """Scripter whose LLM step returns a preset paragraph (no network)."""

    def __init__(self, script: str = _paragraph(70), **kwargs) -> None:
        super().__init__(**kwargs)
        self._scripted = script
        self.llm_calls: list[tuple[int, str]] = []

    def _run_llm(self, content, session_id: str) -> str:
        self.llm_calls.append((len(content.parts), session_id))
        return self._scripted


class FailingScripter(Scripter):
    """Scripter whose LLM step always fails — models a blocked/unreachable key."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.llm_calls = 0

    def _run_llm(self, content, session_id: str) -> str:
        self.llm_calls += 1
        raise RuntimeError("403 PERMISSION_DENIED: API key leaked")


class SpyScripter(Scripter):
    """Records LLM invocations so tests can prove the LLM was skipped."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.llm_calls = 0

    def _run_llm(self, content, session_id: str) -> str:
        self.llm_calls += 1
        return _paragraph(70)


class FakeLocalWriter:
    """Deterministic stand-in for :class:`LocalScriptWriter`."""

    def __init__(self, script: str = _paragraph(70)) -> None:
        self.script = script
        self.calls: list[UserProfile] = []

    def write(self, profile: UserProfile) -> str:
        self.calls.append(profile)
        return self.script


# --- validate_script: the single shape gate ------------------------------------


def test_validate_script_accepts_a_seventy_word_single_paragraph():
    text = _paragraph(70)

    assert validate_script(text) == text


@pytest.mark.parametrize("word_count", [SCRIPTER_MIN_WORDS, SCRIPTER_MAX_WORDS])
def test_validate_script_accepts_the_boundary_word_counts(word_count):
    text = _paragraph(word_count)

    assert validate_script(text) == text


@pytest.mark.parametrize("word_count", [59, 91])
def test_validate_script_rejects_out_of_budget_word_counts(word_count):
    assert validate_script(_paragraph(word_count)) is None


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_validate_script_rejects_empty_or_whitespace(text):
    assert validate_script(text) is None


def test_validate_script_rejects_two_paragraph_blocks():
    text = _paragraph(35) + "\n\n" + _paragraph(35)

    assert validate_script(text) is None


def test_validate_script_tolerates_surrounding_whitespace():
    text = "\n  " + _paragraph(70) + "  \n\n"

    assert validate_script(text) == _paragraph(70)


def test_validate_script_collapses_whitespace_within_one_paragraph():
    text = "  " + _paragraph(69) + "   final\n"

    result = validate_script(text)

    assert result == _paragraph(69) + " final"
    assert "  " not in result
    assert "\n" not in result


@pytest.mark.parametrize(
    "marker",
    [
        "**bold**",
        "- bullet item",
        "`code`",
        "# Heading",
        "---",
        "*emphasis*",
        "> quote",
        "[link](http://example.com)",
    ],
)
def test_validate_script_rejects_markdown(marker):
    text = _paragraph(70) + "\n" + marker

    assert validate_script(text) is None


# --- prompt assembly: one text Content, profile-grounded ------------------------


def test_prompt_content_is_exactly_one_text_part():
    scripter = Scripter(api_key="x")

    content = scripter._prompt_content(_profile())

    assert isinstance(content, genai_types.Content)
    assert len(content.parts) == 1
    assert content.parts[0].inline_data is None
    assert content.parts[0].text is not None


def test_prompt_text_embeds_profile_summary_animal_and_shape_requests():
    scripter = Scripter(api_key="x")
    profile = _profile("the Lone Wolf")

    text = scripter._prompt_content(profile).parts[0].text

    assert profile.summary in text
    assert profile.suggested_animal in text
    assert "Suggested animal:" in text
    assert "one paragraph" in text
    assert "60–90 words" in text
    assert "no markdown" in text


# --- _run_llm text extraction boundary ------------------------------------------


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


def test_run_llm_uses_only_the_final_response_text():
    events = [
        _FakeEvent(final=False, parts=[genai_types.Part(text="interim ignored")]),
        _FakeEvent(final=True, parts=[genai_types.Part(text="The final paragraph.")]),
        _FakeEvent(final=True, parts=None),  # empty final event must be safe
    ]
    scripter = Scripter(api_key="x")
    scripter._runner = _FakeRunner(events)

    result = scripter._run_llm(scripter._prompt_content(_profile()), "1")

    assert result == "The final paragraph."


def test_run_llm_joins_multiple_final_text_parts():
    events = [_FakeEvent(final=True, parts=[genai_types.Part(text="one"), genai_types.Part(text="two")])]
    scripter = Scripter(api_key="x")
    scripter._runner = _FakeRunner(events)

    assert scripter._run_llm(scripter._prompt_content(_profile()), "1") == "one two"


def test_run_llm_with_no_final_text_returns_empty_string():
    events = [_FakeEvent(final=True, parts=[genai_types.Part(text=None)])]
    scripter = Scripter(api_key="x")
    scripter._runner = _FakeRunner(events)

    assert scripter._run_llm(scripter._prompt_content(_profile()), "1") == ""


# --- _run_llm seam: scripted text flows through write_script and is stored -------


def test_write_script_stores_the_scripted_llm_text_on_the_shared_state(caplog):
    store = InterviewStateStore()
    profile = _profile("the Night Owl")
    _save_state(store, 4242, profile)
    script = _paragraph(75)
    scripter = ScriptedScripter(script=script, api_key="x", store=store)

    with caplog.at_level(logging.INFO):
        result = scripter.write_script(4242, profile)

    assert result == script
    assert scripter.llm_calls == [(1, "4242")]  # one single-part Content, one call
    stored = store.get(4242)
    assert stored.script == script
    assert stored.schema_version == SCHEMA_VERSION
    assert stored.phase is InterviewPhase.COMPLETE
    assert stored.profile == profile
    assert stored.answers == [("q1", "a1")]
    messages = [r.message for r in caplog.records]
    assert any("event=script_generated" in m and "source=gemini" in m for m in messages)
    assert any("event=script_stored" in m for m in messages)


def test_stored_script_record_carries_the_current_schema_version():
    """The write never hardcodes the version: it must track SCHEMA_VERSION."""
    store = InterviewStateStore()
    profile = _profile()
    _save_state(store, 20, profile)
    scripter = ScriptedScripter(api_key="x", store=store)

    scripter.write_script(20, profile)

    assert store.get(20).schema_version == SCHEMA_VERSION


def test_every_write_starts_from_a_fresh_reaped_session():
    """Each script is exactly one LLM turn: a reused ADK session would leak the
    previous prompt/paragraph as history. Every write begins (and ends) clean."""
    store = InterviewStateStore()
    profile = _profile()
    _save_state(store, 111, profile)
    scripter = ScriptedScripter(api_key="x", store=store)

    scripter.write_script(111, profile)
    assert scripter._sessions.list_sessions_sync(app_name=APP, user_id="111").sessions == []

    scripter.write_script(111, profile)
    assert scripter._sessions.list_sessions_sync(app_name=APP, user_id="111").sessions == []

    scripter.write_script(222, profile)
    assert scripter._sessions.list_sessions_sync(app_name=APP, user_id="222").sessions == []
    assert scripter._sessions.list_sessions_sync(app_name=APP, user_id="111").sessions == []


# --- local-writer fallback ------------------------------------------------------


def test_llm_failure_uses_local_writer_and_stores_its_script(caplog):
    store = InterviewStateStore()
    profile = _profile("the Lone Wolf")
    _save_state(store, 1, profile)
    local = FakeLocalWriter(script=_paragraph(72))
    scripter = FailingScripter(api_key="x", store=store, local_writer=local)

    with caplog.at_level(logging.INFO):
        result = scripter.write_script(1, profile)

    assert result == _paragraph(72)
    assert local.calls == [profile]
    assert store.get(1).script == _paragraph(72)
    assert any(
        "event=script_generated" in r.message and "source=local" in r.message
        for r in caplog.records
    )


def test_shape_rejected_llm_output_falls_back_and_never_stores_off_spec_text(caplog):
    store = InterviewStateStore()
    profile = _profile()
    _save_state(store, 2, profile)
    local = FakeLocalWriter(script=_paragraph(70))
    scripter = ScriptedScripter(script="Too short.", api_key="x", store=store, local_writer=local)

    with caplog.at_level(logging.WARNING):
        result = scripter.write_script(2, profile)

    assert result == _paragraph(70)
    assert store.get(2).script == _paragraph(70)  # "Too short." never stored
    assert any("event=script_validation_failed" in r.message for r in caplog.records)
    assert _sessions(scripter, 2) == []  # the finally reap still fired


def test_no_local_writer_and_failing_llm_raises_and_leaves_state_untouched():
    store = InterviewStateStore()
    profile = _profile()
    _save_state(store, 3, profile)
    scripter = FailingScripter(api_key="x", store=store)  # no local writer wired

    with pytest.raises(ScripterError):
        scripter.write_script(3, profile)

    assert store.get(3).script is None
    assert _sessions(scripter, 3) == []  # the finally reap fired even on the raise


def test_off_spec_local_writer_raises_loudly_and_leaves_state_untouched(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    store = InterviewStateStore()
    profile = _profile()
    _save_state(store, 4, profile)
    scripter = Scripter(api_key=None, store=store, local_writer=FakeLocalWriter(script="nope"))

    with pytest.raises(ScripterError):
        scripter.write_script(4, profile)

    assert store.get(4).script is None


def test_no_gemini_key_goes_straight_to_the_local_writer(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    store = InterviewStateStore()
    profile = _profile("the Sea Otter")
    _save_state(store, 5, profile)
    local = FakeLocalWriter(script=_paragraph(70))
    scripter = SpyScripter(api_key=None, store=store, local_writer=local)

    result = scripter.write_script(5, profile)

    assert result == _paragraph(70)
    assert scripter.llm_calls == 0  # no keyless LLM round-trip
    assert local.calls == [profile]


def test_timeout_falls_back_to_the_local_writer_within_budget(monkeypatch):
    monkeypatch.setenv("SCRIPTER_GEMINI_TIMEOUT", "0.2")
    store = InterviewStateStore()
    profile = _profile()
    _save_state(store, 9, profile)

    class HangingScripter(Scripter):
        def _run_llm(self, content, session_id: str) -> str:
            time.sleep(1)
            return _paragraph(70)

    local = FakeLocalWriter(script=_paragraph(70))
    scripter = HangingScripter(api_key="x", store=store, local_writer=local)
    started = time.monotonic()

    result = scripter.write_script(9, profile)

    elapsed = time.monotonic() - started
    assert result == _paragraph(70)
    assert elapsed < 0.8  # bounded by the 0.2s timeout, not the 1s sleep
    assert store.get(9).script == _paragraph(70)
    assert _sessions(scripter, 9) == []  # the finally reap fired on the timeout path


class ExplodingSaveStore(InterviewStateStore):
    """Store whose write always fails, to prove store errors are loud."""

    def save(self, chat_id: int, state: InterviewState) -> None:
        raise InterviewStateError("store down")


def test_store_rejecting_the_write_raises_scripter_error():
    store = ExplodingSaveStore()
    scripter = ScriptedScripter(script=_paragraph(70), api_key="x", store=store)

    with pytest.raises(ScripterError):
        scripter.write_script(6, _profile())


# --- typed boundary -------------------------------------------------------------


def test_write_script_rejects_a_non_int_chat_id():
    scripter = Scripter(api_key="x", local_writer=FakeLocalWriter())

    with pytest.raises(ScripterError):
        scripter.write_script("123", _profile())  # type: ignore[arg-type]


def test_write_script_rejects_a_non_profile():
    scripter = Scripter(api_key="x", local_writer=FakeLocalWriter())

    with pytest.raises(ScripterError):
        scripter.write_script(1, {"summary": "x"})  # type: ignore[arg-type]


# --- construction / constants ---------------------------------------------------


def test_constructor_honours_explicit_model():
    scripter = Scripter(api_key="x", model="gemini-custom-text")

    assert scripter.model == "gemini-custom-text"


def test_default_model_is_gemini_flash_lite():
    assert DEFAULT_SCRIPTER_MODEL == "gemini-3.1-flash-lite"
    assert SCRIPTER_MODEL == "gemini-3.1-flash-lite"


def test_resolve_model_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("SCRIPTER_MODEL", raising=False)

    assert resolve_model() == "gemini-3.1-flash-lite"


def test_resolve_model_honours_env_override(monkeypatch):
    monkeypatch.setenv("SCRIPTER_MODEL", "gemini-9.9-test-override")

    assert resolve_model() == "gemini-9.9-test-override"
    assert scripter_module.resolve_model() == "gemini-9.9-test-override"


def test_unavailable_reply_copy_is_locked():
    assert SCRIPTER_REPLY_UNAVAILABLE == (
        "Hang on — the narrator's quill ran dry. Give that another go?"
    )


def test_word_budget_constants_are_locked():
    assert SCRIPTER_MIN_WORDS == 60
    assert SCRIPTER_MAX_WORDS == 90


def test_default_gemini_timeout_is_locked():
    assert Scripter.DEFAULT_GEMINI_TIMEOUT_SECONDS == 60.0
