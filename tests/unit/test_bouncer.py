"""Unit tests for the ADK Bouncer (Phase 2 — plan task 4, RED first).

Offline strategy: the LLM step (``_llm_classify``) is subclassed/scripted so no
network and no Gemini key are needed. The real behaviour under test is the
verdict boundary (``parse_decision``), the per-chat session semantics, and the
key-free local-fallback wiring (real OpenCV detector, committed fixtures).
"""
import time
from pathlib import Path

import pytest

import src.bouncer as bouncer_module
from src.bouncer import (
    BOUNCER_MODEL,
    BOUNCER_REJECTION,
    BOUNCER_UNAVAILABLE_REPLY,
    HUMAN_VERDICT_REPLY,
    NON_HUMAN_VERDICT_REPLY,
    Bouncer,
    parse_decision,
)
from src.local_vision import LocalVisionClassifier

APP = "telegram-documentaries"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PERSON_JPEG = (FIXTURES / "person.jpg").read_bytes()
NON_HUMAN_JPEG = (FIXTURES / "non_human.jpg").read_bytes()


# --- parse_decision: boundary of raw agent output ------------------------------


def test_parse_decision_accepts_plain_strict_json():
    decision = parse_decision('{"human_present": true, "reason": "A clear face is visible."}')

    assert decision.human_present is True
    assert decision.reason == "A clear face is visible."


def test_parse_decision_accepts_false_verdict():
    decision = parse_decision('{"human_present": false, "reason": "Empty beach, no people."}')

    assert decision.human_present is False


def test_parse_decision_tolerates_markdown_fenced_json():
    decision = parse_decision('```json\n{"human_present": true, "reason": "Yes"}\n```')

    assert decision.human_present is True


def test_parse_decision_rejects_safely_on_prose():
    decision = parse_decision("Sorry, I don't see anyone here.")

    assert decision.human_present is False


def test_parse_decision_rejects_safely_on_empty_output():
    assert parse_decision("").human_present is False
    assert parse_decision("   \n\t").human_present is False


def test_parse_decision_rejects_safely_on_non_object_json():
    assert parse_decision("[1, 2, 3]").human_present is False
    assert parse_decision('"just a string"').human_present is False


def test_parse_decision_rejects_safely_on_truncated_json():
    decision = parse_decision('{"human_present": true, "reason": "oops')

    assert decision.human_present is False


def test_parse_decision_rejects_safely_when_required_field_missing():
    decision = parse_decision('{"reason": "no verdict field present"}')

    assert decision.human_present is False


def test_parse_decision_rejects_safely_when_verdict_is_not_bool():
    decision = parse_decision('{"human_present": "maybe", "reason": "x"}')

    assert decision.human_present is False


def test_parse_decision_rejects_safely_for_every_uncertain_verdict_value():
    """Uncertainty is not representable in a strict bool → reject-safe (decision #2)."""
    for raw in (
        '{"human_present": null, "reason": "unsure"}',
        '{"human_present": "uncertain", "reason": "unsure"}',
        '{"human_present": "maybe", "reason": "unsure"}',
        '{"human_present": "probably", "reason": "unsure"}',
    ):
        assert parse_decision(raw).human_present is False


def test_parse_decision_rejects_safely_when_reason_is_not_a_string():
    decision = parse_decision('{"human_present": true, "reason": 42}')

    assert decision.human_present is False


def test_parse_decision_tolerates_extra_model_fields():
    """Untrusted model output may carry extra keys; never fail a clear verdict."""
    decision = parse_decision(
        '{"human_present": true, "reason": "a clear face", "confidence": 0.97, "tags": ["person"]}'
    )

    assert decision.human_present is True
    assert decision.reason == "a clear face"


def test_parse_decision_extracts_json_embedded_in_prose():
    decision = parse_decision('Sure! {"human_present": false, "reason": "just a dog"} hope that helps')

    assert decision.human_present is False


def test_parse_decision_rejects_uncertain_prose_safely():
    decision = parse_decision("I'm not sure whether that is a person or a statue.")

    assert decision.human_present is False


# --- Bouncer: verdict routing with a scripted LLM step --------------------------


class ScriptedBouncer(Bouncer):
    """Bouncer whose LLM step returns preset text (no network, no key)."""

    def __init__(self, raw_model_output: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scripted = raw_model_output
        self.llm_calls: list[tuple[int, str]] = []

    def _llm_classify(self, image_bytes: bytes, session_id: str, mime_type: str) -> str:
        self.llm_calls.append((len(image_bytes), session_id))
        return self._scripted


def test_classify_scripted_approval_returns_true_decision():
    bouncer = ScriptedBouncer('{"human_present": true, "reason": "face"}')

    decision = bouncer.classify(b"\x89PNG-bytes", chat_id=501)

    assert decision.human_present is True
    assert decision.reason == "face"
    assert bouncer.llm_calls == [(len(b"\x89PNG-bytes"), "501")]


def test_classify_scripted_rejection_returns_false_decision():
    bouncer = ScriptedBouncer('{"human_present": false, "reason": "a horse"}')

    decision = bouncer.classify(b"jpeg-bytes", chat_id=502)

    assert decision.human_present is False


def test_classify_rejects_safely_when_model_output_is_garbage():
    bouncer = ScriptedBouncer("hmm, I see a mountain I think")

    decision = bouncer.classify(b"bytes", chat_id=503)

    assert decision.human_present is False


def test_classify_propagates_llm_transport_failures():
    class ExplodingBouncer(Bouncer):
        def _llm_classify(self, image_bytes: bytes, session_id: str, mime_type: str) -> str:
            raise RuntimeError("genai down")

    with pytest.raises(RuntimeError):
        ExplodingBouncer().classify(b"bytes", chat_id=504)


# --- session semantics: one ephemeral session per chat --------------------------


def test_classify_creates_a_session_for_the_chat():
    bouncer = ScriptedBouncer('{"human_present": true, "reason": "face"};')
    bouncer.classify(b"bytes", chat_id=601)

    sessions = bouncer._sessions.list_sessions_sync(app_name=APP, user_id="601").sessions

    assert [s.id for s in sessions] == ["601"]


def test_reset_chat_deletes_only_that_chats_session():
    bouncer = ScriptedBouncer('{"human_present": false, "reason": "no"}')
    bouncer.classify(b"bytes", chat_id=701)
    bouncer.classify(b"bytes", chat_id=702)  # other chat stays alive

    bouncer.reset_chat(701)

    assert bouncer._sessions.list_sessions_sync(app_name=APP, user_id="701").sessions == []
    remaining = bouncer._sessions.list_sessions_sync(app_name=APP, user_id="702").sessions
    assert [s.id for s in remaining] == ["702"]


def test_reset_chat_is_idempotent():
    bouncer = ScriptedBouncer('{"human_present": false, "reason": "no"}')
    bouncer.classify(b"bytes", chat_id=801)

    bouncer.reset_chat(801)
    bouncer.reset_chat(801)  # second call: nothing to delete, must not raise

    assert bouncer._sessions.list_sessions_sync(app_name=APP, user_id="801").sessions == []


def test_reset_chat_for_unknown_chat_is_a_noop():
    bouncer = ScriptedBouncer('{"human_present": false, "reason": "no"}')

    bouncer.reset_chat(999)

    assert bouncer._sessions.list_sessions_sync(app_name=APP, user_id="999").sessions == []


# --- constants ---------------------------------------------------------------


def test_rejection_copy_is_locked():
    assert BOUNCER_REJECTION == (
        "Oi! 📸 No monsters, no sunsets, and definitely no last night's lasagna. "
        "I only do *humans* — a face, a torso, a faintly smug grin. "
        "Send me a picture of a person, mate."
    )


def test_unavailable_reply_copy_is_locked():
    assert BOUNCER_UNAVAILABLE_REPLY == (
        "Hang on — I couldn't get a good look at that photo. Mind sending it again?"
    )


def test_verdict_reply_copies_are_locked():
    assert HUMAN_VERDICT_REPLY == "Human detected ✓"
    assert NON_HUMAN_VERDICT_REPLY == "Non-human detected"


def test_default_model_is_gemini_flash_lite():
    assert BOUNCER_MODEL == "gemini-3.1-flash-lite"


def test_resolve_model_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("BOUNCER_MODEL", raising=False)

    assert bouncer_module.resolve_model() == "gemini-3.1-flash-lite"


def test_resolve_model_honours_env_override(monkeypatch):
    monkeypatch.setenv("BOUNCER_MODEL", "gemini-9.9-test-override")

    assert bouncer_module.resolve_model() == "gemini-9.9-test-override"


# --- key-free local fallback (real OpenCV detector, committed fixtures) --------


class RaisingBouncer(Bouncer):
    """Bouncer whose LLM step always fails — models Gemini being unreachable."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.llm_calls = 0

    def _llm_classify(self, image_bytes: bytes, session_id: str, mime_type: str) -> str:
        self.llm_calls += 1
        raise RuntimeError("genai down")


class SpyBouncer(Bouncer):
    """Records LLM invocations so tests can prove Gemini was skipped."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.llm_calls = 0

    def _llm_classify(self, image_bytes: bytes, session_id: str, mime_type: str) -> str:
        self.llm_calls += 1
        return '{"human_present": true, "reason": "unexpected llm call"}'


@pytest.fixture(scope="module")
def local_classifier() -> LocalVisionClassifier:
    detector = LocalVisionClassifier()
    if not detector.available:
        pytest.skip("Haar cascades unavailable (opencv data missing)")
    return detector


def test_gemini_failure_falls_back_to_local_human_verdict(local_classifier, monkeypatch):
    """Gemini down + person photo → local detector says human, no crash."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    bouncer = RaisingBouncer(api_key="x", local_classifier=local_classifier)

    decision = bouncer.classify(PERSON_JPEG, chat_id=901)

    assert decision.human_present is True
    assert "local" in decision.reason
    assert bouncer.llm_calls == 1  # Gemini WAS attempted before falling back


def test_gemini_failure_falls_back_to_local_non_human_verdict(local_classifier, monkeypatch):
    """Gemini down + landscape photo → local detector says non-human."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    bouncer = RaisingBouncer(api_key="x", local_classifier=local_classifier)

    decision = bouncer.classify(NON_HUMAN_JPEG, chat_id=902)

    assert decision.human_present is False
    assert "non-human" in decision.reason


def test_local_only_mode_skips_gemini_entirely(local_classifier, monkeypatch):
    """No Gemini key + fallback wired → judged locally, LLM never called."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    bouncer = SpyBouncer(api_key=None, local_classifier=local_classifier)

    decision = bouncer.classify(PERSON_JPEG, chat_id=903)

    assert decision.human_present is True
    assert bouncer.llm_calls == 0  # keyless round-trip avoided


def test_gemini_timeout_falls_back_to_local(local_classifier, monkeypatch):
    """A hung Gemini call (observed with flagged keys) must NOT freeze the bot."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("BOUNCER_GEMINI_TIMEOUT", "0.3")

    class HangingBouncer(Bouncer):
        def _llm_classify(self, image_bytes: bytes, session_id: str, mime_type: str) -> str:
            time.sleep(5)
            return '{"human_present": true, "reason": "late"}'

    bouncer = HangingBouncer(api_key="x", local_classifier=local_classifier)
    started = time.monotonic()

    decision = bouncer.classify(PERSON_JPEG, chat_id=904)

    elapsed = time.monotonic() - started
    assert decision.human_present is True
    assert "local" in decision.reason
    assert elapsed < 4  # bounded by the 0.3s timeout, not the 5s sleep