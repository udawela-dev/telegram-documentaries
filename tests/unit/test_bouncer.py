"""Unit tests for the ADK Bouncer (Phase 2 — plan task 4, RED first).

Offline strategy: the LLM step (``_llm_classify``) is subclassed/scripted so no
network and no Gemini key are needed. The real behaviour under test is the
verdict boundary (``parse_decision``) and the per-chat session semantics.
"""
import pytest

from src.bouncer import (
    BOUNCER_MODEL,
    BOUNCER_REJECTION,
    Bouncer,
    parse_decision,
)

APP = "telegram-documentaries"


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
    assert BOUNCER_REJECTION.startswith(
        "Oi! 📸 No monsters, no sunsets, and definitely no last night's lasagna."
    )
    assert "*humans*" in BOUNCER_REJECTION


def test_default_model_is_gemini_flash_lite():
    assert "gemini-3.1-flash-lite" in BOUNCER_MODEL