# Sangam

A bilingual (Hindi/English — Hinglish) phone intake line for PG and rental
accommodation maintenance requests, built for the DataForge × Rime
hackathon (IIT Kharagpur), to survive the way most urban Indian callers
actually talk: mixing Hindi and English mid-sentence, not picking one at
a call's start and staying in it.

**Sangam** (संगम) — the Hindi word for the confluence where two rivers
meet — is the name because that's the literal behavior the system is
built to track and respond to correctly: two languages merging in one
utterance, not running in parallel lanes.

## The problem

PGs, hostels, and rental flats across Indian cities run maintenance
request lines that are either English-only IVRs (residents who default
to Hindi mid-request get mishandled or give up) or a warden/caretaker's
personal phone (doesn't scale, no record, no after-hours coverage).
Language-selection IVRs ("Hindi ke liye 2 dabaayein") don't fix this
either — most Hinglish speakers don't pick a language and hold it for an
entire call. A resident might say *"bhai, bathroom mein paani ki fuga hai,
please jaldi bhejo kisi ko"* in one breath. A system that locked onto
"English" or "Hindi" at minute zero either mishears the mixed clause or
replies in a register the caller didn't use that turn.

**Removing Rime-generated speech breaks this product entirely** — the
whole interaction is a phone call. There is no screen to fall back to.

## The hard voice problem being proven

Two combined, both from the challenge's suggested directions:

1. **Multilingual and code-switched speech** — detecting language at the
   clause level (not call level), maintaining a language state that can
   flip mid-conversation, and rendering mixed-language responses as one
   natural utterance using Rime Arcana's native code-switching, rather
   than choppily swapping voices.
2. **Telephony and conversation continuity** — real phone calls drop.
   Session state (task progress + language state) persists per caller
   phone number and resumes on callback within a recovery window, instead
   of restarting the intake flow and re-asking for information already
   given.

Acceptance test, procedure, and results are in `RIME_EVIDENCE.md`.

## Architecture

```
Caller's phone
    │  (real cellular network, not browser mic)
    ▼
Twilio phone number → SIP trunk
    ▼
LiveKit SIP → LiveKit Agents room (room name = caller's phone number)
    ▼
AgentSession
    ├─ STT: Deepgram nova-3 (multi-language mode)
    ├─ LanguageTracker (agent/language_tracker.py)
    │     — segment-level language tagging (Devanagari script detection +
    │       Romanized-Hindi marker fallback), confirm-streak / high-
    │       confidence commit logic, dominant-language voting, switch
    │       event log
    ├─ SessionStore (agent/session_state.py)
    │     — SQLite, keyed by caller phone number, recovery window = 180s
    ├─ LLM (any chat-completions model; prompts.py builds the system prompt
    │     with live language-mirroring instructions + task-state extraction)
    └─ RimeSpeaker (agent/rime_client.py)
          — wraps LiveKit's `inference.TTS(model="rime/coda", ...)`,
            logs every spoken turn with TTFB, exposes active_provider for
            live observability, handles disclosed fallback
```

## Exact Rime configuration used

| Setting    | Value                                         |
| ---------- | ---------------------------------------------- |
| Model ID   | `coda` (via `rime/coda` in LiveKit inference) |
| Voice      | `taru` — Rime's Coda Hindi voice for India. The earlier `celeste` default is an American English voice and produced an NRI-like accent for Hindi. |
| Language   | `hi` — the line is Hindi-first, so Hindi grammar is written in Devanagari and short English maintenance terms remain inline as natural Hinglish. |
| Audio format | `pcm`, 24kHz |
| Endpoint / transport | LiveKit Cloud SIP + LiveKit Agents `inference.TTS`, telephony audio over the LiveKit SIP bridge to Twilio |

**Why Coda/Taru now**: LiveKit's current Rime inference gateway routes this
line through Coda. `taru` is a verified India Hindi Coda voice, unlike the
previous American-English `celeste` default. Coda does not support a
single voice across Hindi and English, so Sangam keeps Hindi turns in
Devanagari-led Hindi with only common inline maintenance words in English.
The language tracker still detects caller switching; a long full-English
reply needs a separately verified English voice before it can be enabled.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real credentials, never commit .env
```

Telephony wiring (Twilio ↔ LiveKit SIP): see `infra/twilio_setup.md`.

Run the agent worker:
```bash
python -m agent.main dev
```

Run the code-switch acceptance test (no telephony or API keys required —
tests the real `LanguageTracker` class directly):
```bash
python -m eval.run_eval --verbose
```

Run the session-recovery unit tests:
```bash
pytest tests/ -v
```

Verify the Rime API key and listen for code-switch quality (no LiveKit,
Twilio, or Deepgram needed):
```bash
export RIME_API_KEY=your_real_key
python smoke_test_rime.py
```

## Production Hardening & Extended Testing (`hardening-improvements` branch)

To go beyond baseline hackathon requirements, the repository includes a dedicated [`hardening-improvements`](https://github.com/devashishrege18/sangam/tree/hardening-improvements) branch addressing production-grade edge cases in barge-in, async concurrency, and SQLite durability:

1. **Atomic State Fencing (`_state_lock` RLock)**: Guards against check-then-save race conditions if a caller interrupts at the exact millisecond state is written to SQLite.
2. **Concurrent Interruption Serialization (`asyncio.Lock`)**: Serializes simultaneous VAD turn-detection events, eliminating double epoch advances and hardware toggle thrashing.
3. **Audio Frame Queue Accounting**: Dispatches `task_done()` for every flushed audio chunk during interruption, preventing `queue.join()` consumer hangs.
4. **Monotonic Synthesis Generation Counter**: Replaces simple cancellation booleans in `RimeSpeaker` with generation counters to completely prevent audio collisions during rapid caller interruptions.
5. **Multi-Task Set Tracking & Cleanup**: Tracks all in-flight LLM and TTS tasks in `Set[asyncio.Task]` with automatic `done_callback` cleanup, cancelling all active tasks upon barge-in.
6. **Cancellation Diagnostics & End-to-End Latency**: Collects component-level error traces (`last_cancellation_errors`) and measures confirmed task termination time (`wait_for_cancellation()`).
7. **Expanded Test Suite (13 / 13 Passing Tests)**: Adds 6 dedicated concurrency and lock tests in `tests/test_interruption.py` alongside the 7 baseline tests.

To run the extended 13-test suite:
```bash
git checkout hardening-improvements
pytest tests/ -v
```

## Known limitations (disclosed, not hidden)

- The language tagger in `main.py::naive_language_tag` uses two signals:
  Devanagari-script detection (near-unambiguous when present) and a
  Romanized-Hindi keyword list (for transliterated Hindi, which is common
  in casual speech-to-text output). It's a placeholder, not a production
  language-ID model — intentionally isolated behind one function so it
  can be swapped for Deepgram's per-word language output or a
  fastText/langid classifier without touching tracking, state, or TTS
  logic. **The acceptance test evaluates the tracking/decision logic given
  correctly-labeled segments — it does not evaluate raw language-ID
  accuracy from real audio**, which is a distinct, unaddressed claim.
- No SIP trunk failover; a mid-call infra restart drops the call and
  relies on the callback-recovery path, not seamless in-call reconnect.
- **PSTN Telephony Transport**: Real cellular/PSTN telephony via Twilio was provisioned and scripted (`infra/setup_telephony.py`), but live inbound calling via Indian (+91) numbers requires Twilio's regulatory bundle verification (KYC/DoT review), which takes several weeks. LiveKit's WebRTC audio transport was used to verify all core voice claims, code-switching, barge-in, and dropped-session recoveries in this submission instead.
- Fallback TTS provider is not wired in this submission
  (`fallback_tts_session=None`) — Rime failures currently raise rather
  than silently degrade. Flagged here per the "make fallbacks visible"
  rule rather than left undisclosed.
- Only Hindi/English are handled by the language tracker.
- The live Coda/Taru delivery profile is Hindi-first. It has not been
  validated for long, fully English responses, so the prompt deliberately
  keeps Hindi turns concise and avoids full sentence-level code switching.

## Repository layout

```
agent/
  main.py              — LiveKit agent entrypoint, wires everything together
  language_tracker.py  — segment-level code-switch detection (the core claim)
  session_state.py     — SQLite-backed call recovery across dropped calls
  rime_client.py        — Rime TTS wrapper with observability + fallback logging
  prompts.py             — system prompt construction, language-mirroring logic
eval/
  fixtures.jsonl        — code-switch stress-test cases (the acceptance test)
  run_eval.py            — runs fixtures against the real LanguageTracker
tests/
  test_session_recovery.py — pytest suite for the recovery claim
  test_interruption.py     — pytest suite for task interruption, state fencing & cancellation
infra/
  twilio_setup.md        — telephony wiring instructions
smoke_test_rime.py       — standalone Rime API key/voice sanity check
RIME_EVIDENCE.md         — acceptance test, procedure, results, limitations
```

