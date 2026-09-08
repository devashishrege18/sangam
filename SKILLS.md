# SKILLS.md

Repeatable procedures for common tasks on this repository. Each skill
lists: when to use it, the steps, and how to verify it worked. An agent
should follow the relevant skill rather than improvising from scratch.

---

## Skill: Diagnose a failing eval fixture

**When:** `python -m eval.run_eval` reports a fixture as FAIL.

**Steps:**
1. Re-run with `--verbose` to get the per-segment trace:
   `python -m eval.run_eval --verbose`
2. For the failing fixture ID, read its `description` field in
   `eval/fixtures.jsonl` — it states the failure mode the fixture guards
   against. Determine which of two things happened:
   - **The fixture's expectation is wrong** (an authoring mistake) — fix
     the `expect_*` fields in `fixtures.jsonl` and document *why* in the
     fixture's `description` field, inline.
   - **The tracker's actual behavior is wrong** (a real bug) — fix
     `language_tracker.py`, not the fixture.
3. Never silently loosen an expectation to make a fixture pass. If a
   threshold change is needed (`CONFIRM_STREAK`, `HIGH_CONFIDENCE_COMMIT`),
   check it against every other fixture, not just the failing one —
   thresholds are shared global state and a fix for one fixture can break
   another (this happened during initial development; see
   `RIME_EVIDENCE.md` procedure section for the worked example).
4. Re-run the full suite, confirm all fixtures pass, then update
   `RIME_EVIDENCE.md`'s procedure section with what changed and why —
   don't just fix silently and move on.

**Verify:** `python -m eval.run_eval --verbose` shows N/N passed with no
regressions in previously-passing fixtures.

---

## Skill: Add a new code-switch stress-test fixture

**When:** You've identified a new realistic failure mode (a new phrase
pattern, a new noise scenario, a new language pairing) worth guarding
against.

**Steps:**
1. Add a new line to `eval/fixtures.jsonl` (JSONL — one JSON object per
   line, no trailing comma). Required fields: `id` (unique, prefixed
   `cs-`), `description` (states what failure mode this guards against,
   not just what the fixture does), `segments` (list of `{text, lang,
   confidence?}`), and at least one `expect_*` field.
2. Run `python -m eval.run_eval --verbose` and read the trace for your new
   fixture before writing the `expect_*` values — don't guess the
   tracker's behavior, observe it, then decide if that observed behavior
   is correct or a bug (see the fixture-diagnosis skill above).
3. If the fixture reveals a bug, fix `language_tracker.py`, not the
   fixture's expectations.

**Verify:** New fixture passes, all existing fixtures still pass.

---

## Skill: Swap the LLM provider

**When:** Changing from OpenAI to Anthropic, a local Ollama model, or
similar in `agent/main.py`.

**Steps:**
1. The LLM is wired into the `AgentSession` in `agent/main.py`. Swap the
   model client there; don't touch `prompts.py`'s prompt-construction
   logic unless the new model needs different formatting conventions
   (e.g. a model that doesn't follow "one field per turn" instructions as
   reliably may need a stricter, more repetitive system prompt).
2. `prompts.build_system_prompt()` is model-agnostic by design — it
   returns a plain string. Keep it that way; don't add
   provider-specific branching inside it.
3. Re-run a manual conversation test via the LiveKit playground (or
   `agent/main.py`'s dev mode) covering: a plain English request, a plain
   Hindi request, and one mid-sentence code-switch — confirm the new
   model still extracts task fields correctly and doesn't ask for two
   fields in one turn (a common failure mode with weaker models).

**Verify:** Manual conversation test covers all three cases above without
needing to touch `language_tracker.py` or `session_state.py`.

---

## Skill: Verify Rime model/voice/language before a demo or submission

**When:** Before recording the final demo, before submission, or any time
`rime_client.py`'s `RIME_MODEL` or `DEFAULT_VOICE` constants change.

**Steps:**
1. Run `python smoke_test_rime.py` (needs `RIME_API_KEY` set). It
   generates `hello_en.wav`, `hello_hi.wav`, and — most importantly —
   `hello_codeswitch.wav`.
2. Listen to `hello_codeswitch.wav` specifically. It must sound like one
   continuous natural voice, not two voices stitched together. If it
   sounds choppy, the model/voice pairing doesn't support code-switching
   as claimed — do not proceed with that pairing.
3. Cross-check the speaker name against Rime's live voice catalog (not a
   cached list) — the hackathon's eligibility rules disqualify a
   model/voice/language combo that fails event preflight.
4. Update `README.md`'s "Exact Rime configuration used" table if anything
   changed.

**Verify:** All three smoke-test files generate successfully, and the
code-switch sample sounds natural on manual listen.

---

## Skill: Test the dropped-call recovery flow

**When:** Before demo recording, or after any change to
`session_state.py` or `main.py`'s recovery wiring.

**Steps:**
1. Automated check first: `pytest tests/ -v` — confirms the storage layer
   in isolation.
2. Manual/live check (requires LiveKit + Twilio wired): place a real call,
   get partway through the intake flow (at least one field collected),
   then either hang up or put the test phone in airplane mode for
   5-10 seconds.
3. Call back from the same number within 180 seconds
   (`RECOVERY_WINDOW_SECONDS` in `session_state.py`).
4. Confirm on the recording: the agent does NOT re-greet in English or ask
   for already-collected fields again, and it resumes in the language the
   caller was using before the drop.
5. Also test the negative case: call back from a *different* number and
   confirm no state leaks across callers.

**Verify:** Recorded demo clip shows correct resume behavior; negative
case confirms isolation.

---

## Skill: Add a new language beyond Hindi/English

**When:** Extending Sangam past its current Hindi/English (Hinglish)
scope — e.g. adding Tamil, Bengali, or another Arcana-supported language
relevant to a different regional deployment.

**Steps:**
1. Confirm Rime Arcana supports the target language and has a
   code-switching-capable voice for it — check the live catalog, don't
   assume. As of this submission, Arcana v3 supports English, Spanish,
   French, German, Hebrew, Hindi, Japanese, Portuguese, Arabic, and Tamil.
2. `language_tracker.py`'s `Lang` enum needs a new member, and
   `naive_language_tag()` in `main.py` needs a detection path for the new
   language. If the target language has its own script (e.g. Tamil,
   Bengali), prefer script-range detection the way Devanagari detection
   works for Hindi — it's a much stronger signal than keyword lists and
   should be tried first. Remember `naive_language_tag` is explicitly a
   placeholder (see CLAUDE.md rule 6) — this is a good trigger point to
   replace it with a real classifier instead of extending ad-hoc
   detection logic further.
3. Add fixtures to `eval/fixtures.jsonl` covering the new language mixed
   with both English and Hindi, including a rapid-alternation case like
   `cs-03` and a low-confidence-noise case like `cs-04` — those two
   failure modes generalize to any language pair, not just Hindi/English.
4. Update `README.md`'s limitations section to reflect the new scope (or
   remove the "only Hindi/English" limitation entirely once verified).

**Verify:** New fixtures pass; `smoke_test_rime.py` extended with a
sample in the new language sounds natural.

