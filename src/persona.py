"""Phase 8 presenter personas — typed boundary + the single owner of persona copy.

Pipeline position: the Gateway resolves a chat's persona (``/persona`` command,
``PERSONA_DEFAULT`` env) and passes it to the Scripter (narration *tone*) and
the Narrator (TTS *voice* + style instruction). The Bouncer/Interviewer copy
stays locked and persona-independent (user decision).

This module owns every persona copy string the product ships:

* :data:`PERSONA_SCRIPT_TONE` — instruction snippet the Scripter prepends to
  the LLM instruction for the run;
* :data:`PERSONA_NARRATOR_VOICE` — per-persona prebuilt-voice *defaults*
  (``attenborough`` → ``NARRATOR_VOICE`` env semantics, same as
  ``src.narrator.resolve_narrator_voice``; ``irwin`` → ``NARRATOR_VOICE_IRWIN``
  env, default ``Charon``), resolved at call time by
  :func:`resolve_persona_voice`;
* :data:`PERSONA_NARRATOR_INSTRUCTION` — per-persona style instruction for the
  TTS speech config (``attenborough`` is today's ``NARRATOR_PERSONA``);
* :data:`PERSONA_LOCAL_OPENER` — persona flavouring for the key-free
  :class:`src.local_script.LocalScriptWriter` (``attenborough`` is ``""`` so
  Phase 7's output stays byte-for-byte identical).

Typed boundary (SPECS/TECH.md): ``parse_persona`` accepts only the two locked
tokens and returns ``None`` for anything else — a bad token can never enter
state, and free text is never treated as a persona. Imports are stdlib-only:
the Narrator imports *this* module, so a circular import here would break the
pipeline (the parity tests in ``tests/unit/test_persona.py`` lock the strings
that must stay identical to the Narrator's Phase 6 constants).
"""
from __future__ import annotations

import logging
import os
from enum import Enum

logger = logging.getLogger(__name__)


class Persona(str, Enum):
    """The two locked presenters (str-backed for wire/JSON friendliness)."""

    ATTENBOROUGH = "attenborough"
    IRWIN = "irwin"


#: The initial persona for chats that never ran ``/persona`` (locked fallback).
DEFAULT_PERSONA = Persona.ATTENBOROUGH

# --- locked persona copy (single owner module) -----------------------------------

# Instruction snippet prepended to the Scripter's LLM instruction for the run.
PERSONA_SCRIPT_TONE: dict[Persona, str] = {
    Persona.ATTENBOROUGH: (
        "Keep the classic British wildlife-documentary tone: posh, warm, "
        "playful and gently dramatic, like a great nature presenter."
    ),
    Persona.IRWIN: (
        "Channel an exuberant Australian wildlife-warrior presenter: bright, "
        'cheeky, energetic, and open with an excited "Crikey!".'
    ),
}

# Per-persona prebuilt-voice defaults. ``resolve_persona_voice`` applies the
# env overrides at call time: NARRATOR_VOICE for attenborough (exactly
# ``src.narrator.resolve_narrator_voice`` semantics) and NARRATOR_VOICE_IRWIN
# for irwin. The attenborough default equals narrator.DEFAULT_NARRATOR_VOICE.
PERSONA_NARRATOR_VOICE: dict[Persona, str] = {
    Persona.ATTENBOROUGH: "Orus",
    Persona.IRWIN: "Charon",
}

# Per-persona TTS style instruction (single-speaker prebuilt voice either way).
# The attenborough entry is today's NARRATOR_PERSONA, moved here so this module
# is the only place persona copy lives.
PERSONA_NARRATOR_INSTRUCTION: dict[Persona, str] = {
    Persona.ATTENBOROUGH: (
        "Narrate in a classic British wildlife documentary presenter voice: "
        "deep, male, posh British accent. Speak clearly and dramatically."
    ),
    Persona.IRWIN: (
        "Narrate with exuberant Australian wildlife-show energy: upbeat, "
        'cheerful and theatrical, with a natural excited "Crikey!" flair. '
        "Speak clearly and dramatically."
    ),
}

# Persona flavouring for the key-free LocalScriptWriter. The attenborough
# opener is "" so its output is byte-for-byte Phase 7's template; the irwin
# opener keeps the paragraph Crikey-flavoured while staying inside the
# unchanged 60–90 word budget (worst-case canonical animal: 83 + 1 = 84).
PERSONA_LOCAL_OPENER: dict[Persona, str] = {
    Persona.ATTENBOROUGH: "",
    Persona.IRWIN: "Crikey! ",
}


# --- typed parsing ----------------------------------------------------------------


def parse_persona(token: str | None) -> Persona | None:
    """Parse a ``/persona`` argument at the edge — typed, never free text.

    Trims/lowercases and returns ``None`` for anything that is not one of the
    two locked tokens (the caller replies with the locked usage copy; a bad
    token can never enter state).
    """
    if not isinstance(token, str):
        return None
    normalized = token.strip().lower()
    if not normalized:
        return None
    try:
        return Persona(normalized)
    except ValueError:
        return None


def resolve_default_persona() -> Persona:
    """Resolve ``PERSONA_DEFAULT`` — the initial persona for untouched chats.

    Missing env → ``attenborough`` silently; an invalid/blank value → loud
    ``event=persona_default_invalid`` warning + ``attenborough`` fallback
    (never a crash, never an unknown persona in state).
    """
    raw = os.getenv("PERSONA_DEFAULT")
    if raw is None:
        return DEFAULT_PERSONA
    persona = parse_persona(raw)
    if persona is None:
        logger.warning(
            "event=persona_default_invalid value=%r using_default=%s",
            raw,
            DEFAULT_PERSONA.value,
        )
        return DEFAULT_PERSONA
    return persona


def resolve_persona_voice(persona: Persona) -> str:
    """Resolve the prebuilt TTS voice for ``persona`` at call time.

    * ``attenborough`` → ``NARRATOR_VOICE`` env, default ``Orus`` — exactly
      ``src.narrator.resolve_narrator_voice()`` semantics (today's behaviour);
    * ``irwin`` → ``NARRATOR_VOICE_IRWIN`` env, default ``Charon``.

    Raises ``ValueError`` for anything that is not a typed :class:`Persona`
    (typed boundary: free text is never silently mapped to a voice).
    """
    if not isinstance(persona, Persona):
        raise TypeError(f"unknown persona: {persona!r}")
    if persona is Persona.IRWIN:
        return os.getenv(
            "NARRATOR_VOICE_IRWIN", PERSONA_NARRATOR_VOICE[Persona.IRWIN]
        )
    return os.getenv("NARRATOR_VOICE", PERSONA_NARRATOR_VOICE[Persona.ATTENBOROUGH])
