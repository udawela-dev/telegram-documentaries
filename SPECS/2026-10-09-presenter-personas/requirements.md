# Requirements — Phase 8: Presenter personas (creative liberty) + unknown-command hardening

**Branch:** `feature/2026-10-09-presenter-personas` (stacks on PR #8 `feature/2026-10-09-reset-hardening`, tip `1052cee`).

## Context

Phases 1–7 produce a strictly "posh British nature-documentary" product: the
Scripter's one paragraph and the Narrator's voice are locked to that single
tone. To push beyond correctness into personality, the pipeline gains a
**presenter persona** system: the narration's **tone** (Scripter) and the
**TTS voice + style instruction** (Narrator) become selectable per chat,
starting with two personas — the classic British documentary presenter and a
**Steve-Irwin-style "Crikey!" animal-show legend**.

The Bouncer and Interviewer copy stays **locked and persona-independent**
(latest user decision — persona colours Scripter + Narrator only). Selecting a
persona is a chat preference, never a conversation reset: it must survive
`/restart` and `/start` and only affect future script + voice output.

Riding along (user decision, hardening item): **unknown commands** — any text
message starting with `/` that is not `/start`, `/restart`, or `/persona` gets
a locked friendly reply instead of being treated as an interview answer or an
idle-text prompt.

## Scope

Deliver Phase 8 only. Builds on the Phase 7 gateway (phase guards, reset sweep,
duplicate/sequential dispatch, retry hints). No new stages, no persistence
layer (the person's preference is in-memory per `chat_id`, like every other
state — MISSION.md "Out of scope: persistence"), no changes to the Bouncer or
Interviewer copy.

### In scope

- **Typed `Persona`** (`src/persona.py`): `attenborough` and `irwin`, parsed at
  the edge from the command token (typed enum, never free text), plus persona
  copy/voice/instruction mappings and `PERSONA_DEFAULT` env resolution.
- **Persona store:** in-memory `chat_id → Persona` owned by the Gateway
  (mirrors `PortraitStore` ownership); default from `PERSONA_DEFAULT`
  (invalid env value → logged loud + fall back to `attenborough`).
- **`/persona` command** (chat-scoped):
  - `/persona` → locked usage reply listing the choices.
  - `/persona attenborough` / `/persona irwin` → switch + locked confirmation,
    log `event=persona_set`.
  - `/persona <unknown>` → locked usage reply, log `event=persona_unknown_reply`.
  - Allowed at any phase; never touches interview/converter/scripter state.
  - **Persists across `/start` and `/restart`** (it is a preference, not
    conversation memory). `PERSONA_DEFAULT` remains the initial value for chats
    that never switched.
- **Scripter**: `write_script(chat_id, profile, persona)` — the persona's tone
  instruction is added to the LLM instruction; the **one-paragraph, 60–90
  words, no-markdown contract and `validate_script` gate are unchanged** (both
  persona paths must pass the same validator). LocalScriptWriter fallback also
  honours the persona tone.
- **Narrator**: `synthesize(chat_id, script, persona)` — per-persona prebuilt
  voice + style instruction resolved at call time:
  - `attenborough` → existing `resolve_narrator_voice()` (`NARRATOR_VOICE`,
    default `Orus`) + existing posh-British instruction (today's behaviour).
  - `irwin` → `NARRATOR_VOICE_IRWIN` (default `Charon`, overridable) + a
    "Crikey!" style instruction. **No local TTS fallback** (locked decision
    remains): any failure degrades to `NARRATOR_REPLY_UNAVAILABLE`.
- **Unknown-command hardening**: in `_dispatch`/`_handle_text`, any text
  message whose first token starts with `/` and is not `/start`, `/restart`,
  or `/persona` → reply with the new locked `GATEWAY_REPLY_UNKNOWN_COMMAND`,
  log `event=unknown_command`, at **every phase** (IDLE/INTERVIEWING/COMPLETE)
  — it must never be stored as an interview answer.

### Out of scope

- Personas beyond the two (YAGNI — no plugin architecture).
- Persona colouring of Bouncer/Interviewer copy (user decision: no).
- Changing the locked script contract or the no-TTS-fallback decision.
- Persistence/DB, webhooks, non-text commands besides the three handled.

## Contracts

- **Typed boundary:** `Persona` is a `str`-backed enum.
  `parse_persona(token: str) -> Persona | None` — `None` for any unknown token
  (the caller replies with the locked usage copy; a bad token can never enter
  state). `resolve_default_persona() -> Persona` reads `PERSONA_DEFAULT`,
  logs `event=persona_default_invalid` and falls back to `attenborough` on bad
  values.
- **Stage signatures gain a `persona: Persona` parameter**
  (`Scripter.write_script`, `Narrator.synthesize`) — additive, typed; the
  Gateway passes the resolved persona from its store.
- **Copy lives in locked constants** (`src/gateway.py` copy block):
  `PERSONA_REPLY_USAGE`, `PERSONA_REPLY_CONFIRMED_ATTENBOROUGH`,
  `PERSONA_REPLY_CONFIRMED_IRWIN`, `GATEWAY_REPLY_UNKNOWN_COMMAND`. Persona
  tone/voice/instruction strings live in `src/persona.py` (the only module
  allowed to own persona copy).
- **Logging by event** (decorator/event style per TECH.md, never in copy
  strings): `persona_set`, `persona_unknown_reply`, `persona_default_invalid`,
  `unknown_command`, `persona_resolved`.

## User decisions (locked; do NOT re-ask)

1. Exactly **two personas**, selectable per chat: `attenborough`, `irwin`.
2. Persona colours **Scripter + Narrator only**; Bouncer/Interviewer copy stays
   locked.
3. Selection via **`/persona` command** (chat-scoped) + `PERSONA_DEFAULT` env
   default; persists across `/start`/`/restart` until changed.
4. **Unknown-command hardening** rides along: any other `/word` gets the locked
   reply at every phase.
5. Backward compatibility is preserved by construction: no env, no command →
   behaviour identical to Phase 7 (`attenborough`). (Per feature-spec policy,
   this backward-compat decision was confirmed with the user.)
6. `irwin` default voice `Charon` is a **candidate** default — lock it here,
   but the live validation item verifies it produces an audible, distinct
   voice note; if the live check shows it unusable, only the `irwin` voice
   default changes (the mechanism stays).