"""Unit tests for the Phase 8 presenter-persona module (RED first).

``src.persona`` is the **single owner of persona copy**: the typed boundary
(``Persona`` enum + ``parse_persona`` — never free text), the ``PERSONA_DEFAULT``
env resolution (invalid → loud ``persona_default_invalid`` + fall back to
``attenborough``), and the locked copy maps the Scripter/Narrator consume
(``PERSONA_SCRIPT_TONE`` / ``PERSONA_NARRATOR_VOICE`` /
``PERSONA_NARRATOR_INSTRUCTION``). The module imports only the stdlib (the
Narrator imports *it*), so every assertion here is fully offline and
deterministic.
"""
from __future__ import annotations

import logging

import pytest

from src.narrator import (
    DEFAULT_NARRATOR_VOICE,
    NARRATOR_PERSONA,
    resolve_narrator_voice,
)
from src.persona import (
    PERSONA_LOCAL_OPENER,
    PERSONA_NARRATOR_INSTRUCTION,
    PERSONA_NARRATOR_VOICE,
    PERSONA_SCRIPT_TONE,
    Persona,
    parse_persona,
    resolve_default_persona,
    resolve_persona_voice,
)

# The two locked presenters — every persona test parametrises over exactly these.
ALL_PERSONAS = list(Persona)


@pytest.fixture(autouse=True)
def _clean_persona_env(monkeypatch):
    """No test may inherit a developer's persona env exports."""
    monkeypatch.delenv("PERSONA_DEFAULT", raising=False)
    monkeypatch.delenv("NARRATOR_VOICE", raising=False)
    monkeypatch.delenv("NARRATOR_VOICE_IRWIN", raising=False)


# --- typed boundary: the enum ----------------------------------------------------


def test_persona_is_a_str_backed_enum_with_the_two_locked_values():
    assert issubclass(Persona, str)
    assert Persona.ATTENBOROUGH.value == "attenborough"
    assert Persona.IRWIN.value == "irwin"
    assert [p.value for p in Persona] == ["attenborough", "irwin"]


def test_persona_members_compare_equal_to_their_string_values():
    # str-backed so a Persona can be used anywhere the wire value is expected.
    assert Persona.IRWIN == "irwin"
    assert Persona.ATTENBOROUGH == "attenborough"


# --- parse_persona: typed at the edge, never free text ---------------------------


@pytest.mark.parametrize("token", ["attenborough", "irwin"])
def test_parse_persona_accepts_the_canonical_tokens(token):
    assert parse_persona(token) is Persona(token)


@pytest.mark.parametrize(
    "token,expected",
    [
        (" Attenborough", Persona.ATTENBOROUGH),
        ("IRWIN", Persona.IRWIN),
        ("\tIrwin\n", Persona.IRWIN),
        ("  Attenborough  ", Persona.ATTENBOROUGH),
    ],
)
def test_parse_persona_normalizes_case_and_whitespace(token, expected):
    assert parse_persona(token) is expected


@pytest.mark.parametrize(
    "token",
    ["bogus", "", "   ", "orson", "attenborough2", "crikey", "/irwin", "irwin!", "Atten"],
)
def test_parse_persona_returns_none_for_unknown_tokens(token):
    """A bad token can never enter state — the caller replies with usage copy."""
    assert parse_persona(token) is None


def test_parse_persona_returns_none_for_a_non_string():
    assert parse_persona(None) is None  # type: ignore[arg-type]
    assert parse_persona(42) is None  # type: ignore[arg-type]


def test_parse_persona_only_ever_returns_typed_personas():
    """The typed-boundary lock: nothing but a Persona (or None) comes back."""
    for token in ["attenborough", " IRWIN ", "bogus", ""]:
        result = parse_persona(token)
        assert result is None or isinstance(result, Persona)


# --- resolve_default_persona: PERSONA_DEFAULT env ---------------------------------


def test_resolve_default_persona_is_attenborough_without_env():
    assert resolve_default_persona() is Persona.ATTENBOROUGH


@pytest.mark.parametrize(
    "value,expected",
    [
        ("irwin", Persona.IRWIN),
        ("attenborough", Persona.ATTENBOROUGH),
        (" IRWIN ", Persona.IRWIN),
        ("Irwin", Persona.IRWIN),
    ],
)
def test_resolve_default_persona_honours_the_env(monkeypatch, value, expected):
    monkeypatch.setenv("PERSONA_DEFAULT", value)

    assert resolve_default_persona() is expected


def test_resolve_default_persona_logs_and_falls_back_on_an_invalid_env(monkeypatch, caplog):
    monkeypatch.setenv("PERSONA_DEFAULT", "bogus")

    with caplog.at_level(logging.WARNING):
        result = resolve_default_persona()

    assert result is Persona.ATTENBOROUGH
    assert any("event=persona_default_invalid" in r.message for r in caplog.records)
    assert any("bogus" in r.getMessage() for r in caplog.records)


def test_resolve_default_persona_falls_back_on_a_blank_env(monkeypatch, caplog):
    monkeypatch.setenv("PERSONA_DEFAULT", "   ")

    with caplog.at_level(logging.WARNING):
        result = resolve_default_persona()

    assert result is Persona.ATTENBOROUGH
    assert any("event=persona_default_invalid" in r.message for r in caplog.records)


def test_resolve_default_persona_reads_the_env_at_call_time(monkeypatch):
    """A running deployment's override applies without a restart."""
    monkeypatch.setenv("PERSONA_DEFAULT", "irwin")
    assert resolve_default_persona() is Persona.IRWIN

    monkeypatch.setenv("PERSONA_DEFAULT", "attenborough")
    assert resolve_default_persona() is Persona.ATTENBOROUGH


# --- parity: every persona has entries in every copy/voice/instruction map --------


def test_every_persona_has_an_entry_in_every_persona_map():
    for persona in ALL_PERSONAS:
        assert persona in PERSONA_SCRIPT_TONE, f"missing script tone for {persona}"
        assert persona in PERSONA_NARRATOR_VOICE, f"missing narrator voice for {persona}"
        assert persona in PERSONA_NARRATOR_INSTRUCTION, (
            f"missing narrator instruction for {persona}"
        )
        assert persona in PERSONA_LOCAL_OPENER, f"missing local opener for {persona}"


def test_all_persona_copy_is_non_empty_except_the_attenborough_local_opener():
    # The attenborough local opener is deliberately "" so Phase 7's
    # LocalScriptWriter output stays byte-for-byte identical.
    for persona in ALL_PERSONAS:
        assert PERSONA_SCRIPT_TONE[persona].strip()
        assert PERSONA_NARRATOR_VOICE[persona].strip()
        assert PERSONA_NARRATOR_INSTRUCTION[persona].strip()
    assert PERSONA_LOCAL_OPENER[Persona.ATTENBOROUGH] == ""


# --- copy lock: the persona copy strings are locked -------------------------------


def test_persona_script_tone_copy_is_locked():
    assert PERSONA_SCRIPT_TONE[Persona.ATTENBOROUGH] == (
        "Keep the classic British wildlife-documentary tone: posh, warm, "
        "playful and gently dramatic, like a great nature presenter."
    )
    assert PERSONA_SCRIPT_TONE[Persona.IRWIN] == (
        "Channel an exuberant Australian wildlife-warrior presenter: bright, "
        'cheeky, energetic, and open with an excited "Crikey!".'
    )


def test_persona_narrator_instruction_copy_is_locked():
    assert PERSONA_NARRATOR_INSTRUCTION[Persona.ATTENBOROUGH] == (
        "Narrate in a classic British wildlife documentary presenter voice: "
        "deep, male, posh British accent. Speak clearly and dramatically."
    )
    assert PERSONA_NARRATOR_INSTRUCTION[Persona.IRWIN] == (
        "Narrate with exuberant Australian wildlife-show energy: upbeat, "
        'cheerful and theatrical, with a natural excited "Crikey!" flair. '
        "Speak clearly and dramatically."
    )


def test_persona_narrator_voice_defaults_are_locked():
    assert PERSONA_NARRATOR_VOICE == {
        Persona.ATTENBOROUGH: "Orus",
        Persona.IRWIN: "Charon",
    }


def test_local_opener_copy_is_locked():
    assert PERSONA_LOCAL_OPENER[Persona.ATTENBOROUGH] == ""
    assert PERSONA_LOCAL_OPENER[Persona.IRWIN] == "Crikey! "


# --- parity with the Phase 6/7 narrator constants (no drift between modules) ------


def test_attenborough_instruction_is_exactly_the_phase6_narrator_persona():
    """persona.py owns the copy; ``NARRATOR_PERSONA`` must be the same string."""
    assert PERSONA_NARRATOR_INSTRUCTION[Persona.ATTENBOROUGH] == NARRATOR_PERSONA


def test_attenborough_voice_default_matches_the_phase6_default_voice():
    assert PERSONA_NARRATOR_VOICE[Persona.ATTENBOROUGH] == DEFAULT_NARRATOR_VOICE == "Orus"


# --- resolve_persona_voice: call-time env semantics -------------------------------


def test_resolve_persona_voice_attenborough_matches_resolve_narrator_voice(monkeypatch):
    assert resolve_persona_voice(Persona.ATTENBOROUGH) == resolve_narrator_voice()
    monkeypatch.setenv("NARRATOR_VOICE", "Fenrir")
    assert resolve_persona_voice(Persona.ATTENBOROUGH) == resolve_narrator_voice() == "Fenrir"


def test_resolve_persona_voice_irwin_defaults_to_charon():
    assert resolve_persona_voice(Persona.IRWIN) == "Charon"


def test_resolve_persona_voice_irwin_honours_the_env_override(monkeypatch):
    monkeypatch.setenv("NARRATOR_VOICE_IRWIN", "Aoife")

    assert resolve_persona_voice(Persona.IRWIN) == "Aoife"


def test_resolve_persona_voice_reads_the_env_at_call_time(monkeypatch):
    assert resolve_persona_voice(Persona.IRWIN) == "Charon"
    monkeypatch.setenv("NARRATOR_VOICE_IRWIN", "Puck")
    assert resolve_persona_voice(Persona.IRWIN) == "Puck"


def test_resolve_persona_voice_rejects_free_text():
    """Typed boundary: only a Persona can be resolved — never free text."""
    with pytest.raises(TypeError):
        resolve_persona_voice("irwin")  # type: ignore[arg-type]
