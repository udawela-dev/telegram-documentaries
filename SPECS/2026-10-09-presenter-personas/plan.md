# Plan — Phase 8: Presenter personas + unknown-command hardening

Red/Green TDD throughout. Run the repo's own scripts for checks:
`scripts/test`, then `scripts/hooks` (compileall + full suite), as documented
in the README. Branch stacks on `feature/2026-10-09-reset-hardening`
(PR #8); do not touch Bouncer/Interviewer copy.

## Task group 1 — Typed `Persona` module (`src/persona.py`)

- `class Persona(str, Enum)` with `ATTENBOROUGH = "attenborough"`,
  `IRWIN = "irwin"`.
- `parse_persona(token: str) -> Persona | None` — trims/lowercases, returns
  `None` for anything unknown (typed parse at the edge; no regex, no free
  text).
- `resolve_default_persona() -> Persona` — `PERSONA_DEFAULT` env (default
  `attenborough`); invalid env value → `logger.warning("event=persona_default_invalid ...")`
  and fall back to `attenborough`.
- Locked copy (single owner module): `PERSONA_SCRIPT_TONE: dict[Persona, str]`
  (instruction snippet for the Scripter LLM), `PERSONA_NARRATOR_VOICE:
  dict[Persona, str]` (attenborough → `resolve_narrator_voice()` behaviour,
  irwin → `os.getenv("NARRATOR_VOICE_IRWIN", "Charon")`), and
  `PERSONA_NARRATOR_INSTRUCTION: dict[Persona, str]` (style instruction for the
  TTS speech config; irwin = aussie "Crikey!" wildlife-show energy, still a
  single-speaker prebuilt voice call).
- Tests first (`tests/unit/test_persona.py`): enum values; parse accepts
  `attenborough`/`irwin` (+ case/whitespace variants) and returns `None` for
  unknown tokens; `resolve_default_persona` honours env, logs
  `persona_default_invalid` + falls back on garbage, defaults to `attenborough`
  with no env; every `Persona` has entries in all three copy/voice maps
  (parity test); voice/instruction copy is locked (snapshot-style assertions).

## Task group 2 — Gateway: `/persona` command + persona store + unknown commands

- Gateway holds `_personas: dict[int, Persona]` and a
  `_resolve_persona(chat_id) -> Persona` helper (store lookup, else
  `resolve_default_persona()`); `_handle_persona_command(chat_id, text, update_id)`
  handles `/persona` (no arg → `PERSONA_REPLY_USAGE`) and `/persona <token>`
  (valid → store + `PERSONA_REPLY_CONFIRMED_*` + `event=persona_set`;
  invalid → `PERSONA_REPLY_USAGE` + `event=persona_unknown_reply`).
- `_handle_persona_command` is safe at any phase and is **not** cleared by
  `_handle_reset_command` (preference, not conversation memory).
- **Unknown commands:** in the text/command routing (gateway dispatch), the
  first token starting with `/` is matched against the known commands
  (`/start`, `/restart`, `/persona`); anything else → `GATEWAY_REPLY_UNKNOWN_COMMAND`
  + `event=unknown_command` at every phase, before interview-answer handling.
- Locked copy additions in the gateway copy block:
  `PERSONA_REPLY_USAGE`, `PERSONA_REPLY_CONFIRMED_ATTENBOROUGH`,
  `PERSONA_REPLY_CONFIRMED_IRWIN`, `GATEWAY_REPLY_UNKNOWN_COMMAND`.
- Tests first (`tests/unit/test_gateway.py`, persona section): `/persona` no
  arg → usage; `/persona irwin` → confirmed + store set; `/persona attenborough`
  → confirmed + store set; `/persona bogus` → usage + `persona_unknown_reply`;
  unknown `/foo` at IDLE, INTERVIEWING and COMPLETE → `GATEWAY_REPLY_UNKNOWN_COMMAND`,
  never an interview answer (question_index unchanged), never a portrait
  prompt; `/persona` before/after `/restart` → persona persists; per-chat
  isolation (two chats, different personas); `_resolve_persona` falls back to
  env default.

## Task group 3 — Scripter persona tone

- `write_script(chat_id, profile, persona)` — append `PERSONA_SCRIPT_TONE[persona]`
  to the LLM instruction; `LocalScriptWriter` gains the same persona tone when
  it is the generation path. `validate_script` and the one-paragraph/60–90-word
  contract are **unchanged** — the same gate covers both personas.
- Tests first (`tests/unit/test_scripter.py`): the prompt/instruction sent to
  the ADK agent contains the persona tone for each persona (spy on agent
  instruction); a persona-toned script still passes `validate_script`;
  `LocalScriptWriter` output differs by persona and stays within budget;
  existing tests updated for the new `persona` parameter.

## Task group 4 — Narrator persona voice + instruction

- `synthesize(chat_id, script, persona)` — resolve voice from
  `PERSONA_NARRATOR_VOICE[persona]` and style instruction from
  `PERSONA_NARRATOR_INSTRUCTION[persona]` into the existing speech config /
  instruction. No local fallback (unchanged). Existing failure paths untouched.
- Tests first (`tests/unit/test_narrator.py`): voice + instruction passed to
  the speech config differ per persona (spy); `attenborough` equals today's
  values (`NARRATOR_VOICE`/locked instruction); `NARRATOR_VOICE_IRWIN` env
  overrides the irwin default; conversion/failure paths unchanged for both.

## Task group 5 — Integration, live tests, docs

- `tests/component/test_gateway_personas.py` (or extend phase7 component file):
  full poll-driven scenarios — `/persona irwin` then a complete run produces
  the irwin-toned script and a voice note; persona survives `/restart` then a
  second run; `/foo` mid-interview never advances the interview; two chats with
  different personas stay isolated.
- Guarded live test (skips without `RUN_LIVE_GEMINI=1` + healthy key):
  `tests/integration/test_live_personas.py` — real TTS for `irwin`: asserts
  audible bytes + OGG/Opus magic and a verifiably **different** voice id from
  `attenborough`'s run (records the candidate `Charon` default; if unusable,
  the spec's user decision 6 allows changing only the irwin default voice).
- Docs (verifier confirms final wording): `SPECS/ROADMAP.md` Phase 8 entry
  (mark implemented + branch + notes), `SPECS/TECH.md` (persona system,
  `/persona`, unknown-command guard), `README.md` (phase list bullet +
  behaviour-table rows for `/persona` and unknown commands), and the three
  spec files updated to what was actually built.
- Run `scripts/test`, then `scripts/hooks`; no claims of passing without real
  runs.