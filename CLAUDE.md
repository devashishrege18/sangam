# CLAUDE.md

Context for any AI coding agent (Claude Code, Antigravity, or similar)
working on this repository. Read this before making changes.

## What this project is

**Sangam** — a bilingual (Hindi/English, Hinglish) phone intake agent for
a PG/rental accommodation's maintenance requests, built for the DataForge
× Rime hackathon (IIT Kharagpur). The product's entire reason to exist is
proving two hard voice-engineering claims:

1. **Code-switch detection at the clause level** — real bilingual callers
   mix languages mid-sentence. The system tracks language state
   per-segment, not per-call, and mirrors the caller's actual switching
   pattern via Rime Arcana's native code-switching.
2. **Session continuity across dropped calls** — telephony drops calls
   constantly. Task progress and language state persist per caller phone
   number and resume on callback within a 3-minute window.

If a change makes either claim weaker or harder to verify, treat that as
a regression even if tests still pass syntactically.

## Repository layout (do not restructure without reason)

```
agent/
  main.py              LiveKit agent entrypoint — wires everything together
  language_tracker.py  Core code-switch detection logic (the main claim)
  session_state.py     SQLite-backed call recovery
  rime_client.py        Rime TTS wrapper: observability + disclosed fallback
  prompts.py            System prompt construction, language mirroring
eval/
  fixtures.jsonl        Code-switch stress-test fixtures (the acceptance test)
  run_eval.py            Runs fixtures against the REAL LanguageTracker
tests/
  test_session_recovery.py   pytest suite for the recovery claim
infra/
  twilio_setup.md        Telephony wiring notes
smoke_test_rime.py       Standalone Rime API key/voice sanity check
README.md                Architecture, setup, exact Rime config
RIME_EVIDENCE.md         Acceptance test, procedure, results, limitations
```

## Ground rules for agents working in this repo

1. **Never fabricate a passing test result.** If you change
   `language_tracker.py`, actually run `python -m eval.run_eval --verbose`
   and `pytest tests/ -v` and paste the real output. `RIME_EVIDENCE.md`
   documents a real failure-diagnose-fix cycle from development — that
   honesty is part of what this submission is judged on. Don't retroactively
   clean fixture history to look like everything always passed.

2. **`eval/run_eval.py` must import the real `LanguageTracker` class**,
   never a reimplementation or mock. If you're tempted to write a
   separate "test version" of the tracker, stop — that defeats the point
   of the acceptance test.

3. **Credentials never go in code, docs, commits, or screenshots.**
   `.env` is gitignored; `.env.example` holds placeholders only. Before
   any commit, grep for `sk-`, `AC[0-9a-f]`, or hardcoded keys.

4. **Confidence thresholds are documented, not arbitrary.** If you touch
   `HIGH_CONFIDENCE_COMMIT`, `CONFIRM_STREAK`, or `RECOVERY_WINDOW_SECONDS`,
   update the docstring explaining *why*, and check it against
   `eval/fixtures.jsonl` — especially `cs-03` (rapid alternation) and
   `cs-04` (low-confidence noise), which are the two fixtures that exist
   specifically to catch regressions in that threshold logic.

5. **Rime model/voice/language must match the live catalog.** Don't trust
   a hardcoded speaker name without verifying it against Rime's current
   catalog — the hackathon rules explicitly penalize a stale speaker list
   that fails preflight. Model in use: `arcana` (native multilingual
   code-switching). Do not swap to Coda or Mist without updating
   `README.md`'s rationale table — those don't code-switch.

6. **`naive_language_tag()` in `main.py` is a known placeholder**, isolated
   deliberately so it can be swapped for a real language-ID classifier
   (Deepgram per-word tags, fastText, langid) without touching
   `language_tracker.py`, `session_state.py`, or `rime_client.py`. If you
   improve it, keep that isolation — don't let classifier logic leak into
   the tracker.

7. **Disclose, don't hide, limitations.** If you add a feature with a known
   gap, add it to the "Known limitations" section of `README.md` and
   `RIME_EVIDENCE.md` in the same change. Silent gaps are a judging risk
   (see hackathon eligibility rules on unverified claims).

## How to verify your work before calling it done

```bash
# Code-switch detection (no API keys needed)
python -m eval.run_eval --verbose

# Session recovery (no API keys needed)
pytest tests/ -v

# Rime API connectivity + voice quality (needs RIME_API_KEY)
python smoke_test_rime.py

# Full agent (needs RIME_API_KEY, DEEPGRAM_API_KEY, LIVEKIT_URL/KEY/SECRET)
python -m agent.main dev
```

If you can't run the full agent (missing LiveKit/Deepgram keys), the first
three checks are still sufficient to verify a `language_tracker.py`,
`session_state.py`, or `rime_client.py` change — they don't require
telephony infra.

## Style

- Plain, direct docstrings that explain *why* a design choice was made,
  not just what the code does — this repo is partly judged on
  reproducibility and reasoning, not just working code.
- No speculative abstraction. This is a hackathon-scoped repo for one
  product; don't add plugin systems, config frameworks, or
  multi-tenant scaffolding "for future flexibility."
- Prefer a failing, documented test over a passing, dishonest one.

