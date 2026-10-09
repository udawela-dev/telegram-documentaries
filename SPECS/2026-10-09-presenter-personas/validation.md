# Validation — Phase 8: Presenter personas + unknown-command hardening

All offline items must pass via the project's own scripts (`scripts/test`,
`scripts/hooks`); every item is ticked only on **actual** execution.

## Offline acceptance (CI-runnable)

- [x] `scripts/test` green (new unit + component + integration tests included).
- [x] `scripts/hooks` green.
- [x] Unit (`tests/unit/test_persona.py`): typed `Persona` parse — valid tokens
      (`attenborough`, `irwin`, case/whitespace variants) resolve; unknown
      tokens return `None`; `PERSONA_DEFAULT` honoured, invalid env logged
      (`persona_default_invalid`) and falls back to `attenborough`; copy/voice/
      instruction parity for every persona; copy locked.
- [x] Unit (gateway): `/persona` (no arg → usage), `/persona <valid>` →
      confirmation + store set per chat, `/persona <invalid>` → usage +
      `persona_unknown_reply`; persona **persists across `/restart`/`/start`**
      and is per-chat isolated; `_resolve_persona` falls back to env default.
- [x] Unit (gateway): **unknown commands** — `/foo` etc. at IDLE, INTERVIEWING
      and COMPLETE get `GATEWAY_REPLY_UNKNOWN_COMMAND` (`event=unknown_command`)
      and are **never stored as interview answers** (question_index unchanged)
      nor treated as text prompts.
- [x] Unit (scripter): persona tone present in the ADK instruction for each
      persona; persona-toned output still passes `validate_script` (one
      paragraph, 60–90 words); `LocalScriptWriter` honours the tone;
      signatures updated.
- [x] Unit (narrator): per-persona voice + instruction reach the speech config;
      `attenborough` == today's values; `NARRATOR_VOICE_IRWIN` override works;
      failure/conversion paths unchanged for both personas.
- [x] Component: full poll-driven run with `/persona irwin` produces irwin-toned
      script + voice note; persona survives restart + second run; `/foo`
      mid-interview never advances; two chats stay isolated.
- [x] Regression: Phase 7 suite remains green with the new `persona` parameters
      (no behavioural change for the default persona).

## Live acceptance (needs the running bot + healthy key + a Telegram user)

- [ ] `/persona` in the user's chat: usage reply lists both presenters; switching
      to `irwin` changes the **spoken voice** of the next voice note (distinct
      from the `attenborough` run) and the script tone reads "Crikey!"-flavoured.
- [ ] `/persona` persists through `/restart`: the restarted run still narrates
      with the chosen persona.
- [ ] `/something-fake` at any phase → locked unknown-command reply, polling
      continues, nothing else changes.
- [ ] `PERSONA_DEFAULT=irwin` (or absent/invalid) behaves per spec on a fresh
      chat.
- [x] Guarded live TTS test confirms the `irwin` default voice (`Charon`)
      produces an audible, playable, distinct voice note; if not, only the
      irwin voice default is adjusted (user decision 6).

## Spec drift

- [x] `ROADMAP.md` Phase 8, `TECH.md` (persona system + `/persona` +
      unknown-command guard), `README.md` (phase bullet + behaviour-table rows)
      updated to reflect what was actually implemented; spec files corrected
      on any drift, after user approval.