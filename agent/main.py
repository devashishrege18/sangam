"""
main.py

Sangam agent entrypoint. Run via `python -m agent.main dev` (LiveKit CLI)
against a LiveKit Agents worker connected to a Twilio SIP trunk. See
infra/twilio_setup.md for the phone-number-to-LiveKit wiring.

Call flow (LiveKit Agents v1.8):
  Twilio SIP -> LiveKit room -> AgentSession.start(agent=SangamAgent)
    STT (Deepgram nova-3, language="multi")
       -> SangamAgent.on_user_turn_completed()  [per-turn hook, fires BEFORE LLM]
          -> naive_language_tag() on the committed transcript
          -> LanguageTracker.ingest()
          -> Agent.update_instructions() so the LLM sees the correct language
             context on THIS turn, not the next
    LLM (Google Gemini Flash) with build_system_prompt(plan, task_state)
       -> streams tokens directly into session pipeline
    TTS (Rime Coda/Taru, Hindi-first) — session streams TTS audio chunk-by-chunk to
       the room so first audio reaches the caller in < 500 ms; no manual
       speak() call needed

Key design decisions:
- We subclass Agent rather than using session.on("user_speech_committed").
  The on_user_turn_completed hook is called synchronously before the LLM
  request starts, so the updated instructions are part of the CURRENT
  response, not the next one. The old "session.instructions = ..." pattern
  in a user_speech_committed handler updated instructions a turn too late.
- session.say() is used only for the opening/recovery greeting; after that
  the pipeline (STT -> LLM -> TTS) handles all speech automatically.
- naive_language_tag() is a placeholder tagger. Swap for Deepgram's
  per-word language metadata (available in nova-3 multi-language mode) or
  a fastText/langid call before relying on this for production accuracy.
  The eval fixtures test the LanguageTracker logic; the tagger is a
  separate concern, also tested separately.

Every turn is persisted via SessionStore keyed by caller phone number, so a
dropped call that reconnects within RECOVERY_WINDOW_SECONDS resumes state
instead of restarting the intake flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from dotenv import load_dotenv
load_dotenv()  # load .env before LiveKit reads LIVEKIT_URL


from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, llm
from livekit.plugins import deepgram, openai, silero

from agent.cancellation import InterruptionOrchestrator, StaleTurnError
from agent.language_tracker import Lang, LanguageState, LanguageTracker
from agent.prompts import build_system_prompt
from agent.rime_client import RimeSpeaker, create_rime_tts
from agent.session_state import SessionStore

logger = logging.getLogger("sangam.agent")

store = SessionStore()


# ---------------------------------------------------------------------------
# Per-segment language tagger
# ---------------------------------------------------------------------------
# Two signals, deliberately not treated as equally reliable:
#
# 1. DEVANAGARI SCRIPT — if Deepgram's STT is configured for Hindi output,
#    Hindi segments often come back in Devanagari (देवनागरी), not
#    transliterated. Detecting that is a hard, near-zero-ambiguity signal
#    (unlike languages that share the Latin alphabet and need keyword
#    heuristics) — so a Devanagari hit gets a high confidence score
#    directly from script alone.
# 2. ROMANIZED HINDI MARKERS — just as often, Hindi comes through
#    transliterated into Latin script ("bhai", "nahi", "kal", "abhi"),
#    especially if the STT is running in English mode or the caller is
#    genuinely code-switching word-by-word rather than clause-by-clause.
#    This is the ambiguous case that needs a keyword list.

_DEVANAGARI_RANGE = re.compile(r"[\u0900-\u097F]")
_ROMAN_HI_MARKERS = re.compile(
    r"\b(hai|nahi|nahin|haan|kya|kal|aaj|abhi|bhai|didi|ghar|kaam|theek|"
    r"thik|karo|kijiye|chalega|paani|bijli|turant|jaldi|samasya|kharab|"
    r"bahut|zyada|kripya|dhanyavaad|namaste)\b", re.IGNORECASE
)

_ROOM_DIGITS = re.compile(r"\b(\d{2,4})\b")
_HINDI_NUMBERS = {
    "एक सौ एक": "101", "एक सौ दो": "102", "एक सौ तीन": "103", "एक सौ चार": "104", "एक सौ पांच": "105", "एक सौ छह": "106",
    "दो सौ एक": "201", "दो सौ दो": "202", "दो सौ तीन": "203", "दो सौ चार": "204",
    "तीन सौ एक": "301", "तीन सौ दो": "302", "तीन सौ तीन": "303", "तीन सौ चार": "304",
    "चार सौ एक": "401", "चार सौ दो": "402", "चार सौ तीन": "403", "चार सौ चार": "404", "चार सौ पाँच": "405",
}


def extract_task_fields(text: str, state: dict) -> dict:
    """Extract maintenance intake fields from user speech turns to persist in session state."""
    updated = {}
    lower_text = text.lower()

    # Room / Unit number
    if "room_number" not in state:
        for hindi_phrase, num in _HINDI_NUMBERS.items():
            if hindi_phrase in text:
                updated["room_number"] = num
                break
        if "room_number" not in updated:
            m = _ROOM_DIGITS.search(text)
            if m:
                updated["room_number"] = m.group(1)

    # Issue Category
    if "issue_category" not in state:
        if any(w in lower_text or w in text for w in ["पानी", "paani", "leak", "plumbing", "pipe", "pipeline", "tap", "bathroom", "नल", "flush", "sink"]):
            updated["issue_category"] = "plumbing"
        elif any(w in lower_text or w in text for w in ["light", "power", "बिजली", "electric", "spark", "current", "switch", "wiring"]):
            updated["issue_category"] = "electrical"
        elif any(w in lower_text or w in text for w in ["ac", "cooling", "गर्मी", "cooler", "fan", "पंखा", "heating", "हीटर"]):
            updated["issue_category"] = "heating_cooling"
        elif any(w in lower_text or w in text for w in ["geyser", "गीजर", "fridge", "microwave", "washing machine", "appliance"]):
            updated["issue_category"] = "appliance"

    # Issue Description
    if "issue_description" not in state and ("issue_category" in updated or "issue_category" in state):
        updated["issue_description"] = text.strip()

    # Urgency
    if "urgency" not in state:
        if any(w in lower_text or w in text for w in ["emergency", "इमरजेंसी", "flooding", "sparking", "आग", "धुआं", "खतरा"]):
            updated["urgency"] = "emergency"
        elif any(w in lower_text or w in text for w in ["urgent", "अर्urgent", "तुरंत", "जल्दी", "asap", "आज ही", "today"]):
            updated["urgency"] = "urgent"
        elif any(w in lower_text or w in text for w in ["routine", "normal", "नॉर्मल", "जब टाइम मिले"]):
            updated["urgency"] = "routine"

    return updated


def naive_language_tag(text: str) -> tuple[Lang, float]:
    words = text.split()
    if not words:
        return Lang.UNKNOWN, 0.0

    if _DEVANAGARI_RANGE.search(text):
        # Script alone is close to unambiguous for Hindi vs. English.
        return Lang.HI, 0.9

    hi_hits = len(_ROMAN_HI_MARKERS.findall(text))
    ratio = hi_hits / max(len(words), 1)
    if ratio == 0:
        return Lang.EN, 0.7
    if ratio > 0.4:
        return Lang.HI, min(0.9, 0.5 + ratio)
    return Lang.EN, 0.6


# ---------------------------------------------------------------------------
# Agent subclass — with low-latency interruption hooks & state fencing
# ---------------------------------------------------------------------------

class SangamAgent(Agent):
    """
    Subclassing Agent lets us override on_user_turn_completed(), which is
    the ONLY hook that fires between the user finishing speaking and the LLM
    sending its first token. Using it here means the language-adaptive system
    prompt is part of the CURRENT response, not the next one.

    Low-Latency Interruption & State Fencing:
    - Integrates InterruptionOrchestrator for <200ms audio buffer truncation.
    - Fences asynchronous tool execution (e.g., ticket creation) and durable
      SQLite session persistence against obsolete turns.
    """

    def __init__(
        self,
        *,
        tracker: LanguageTracker,
        task_state: dict,
        phone_number: str,
        call_sid: str,
        speaker: RimeSpeaker,
        orchestrator: InterruptionOrchestrator | None = None,
    ):
        initial_plan = tracker.response_language_plan()
        super().__init__(
            instructions=build_system_prompt(initial_plan, task_state),
            stt=deepgram.STT(model="nova-3", language="multi"),
            # LLM: Google Gemini Flash via OpenAI-compatible endpoint.
            # GOOGLE_API_KEY + GEMINI_MODEL set in .env; no OPENAI_API_KEY needed.
            # gemini-flash-lite-latest is fast enough for real-time voice (< 600 ms TTFT).
            llm=openai.LLM(
                model=os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"),
                api_key=os.environ.get("GOOGLE_API_KEY"),
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                temperature=0.3,
            ),
            # Taru is Rime's India Hindi Coda voice. Keeping the session language
            # Hindi ensures Devanagari-led answers keep natural local prosody.
            tts=create_rime_tts(),
            vad=silero.VAD.load(),
        )
        self.tracker = tracker
        self.task_state = task_state
        self.phone_number = phone_number
        self.call_sid = call_sid
        self.speaker = speaker
        self.orchestrator = orchestrator or InterruptionOrchestrator(speaker=speaker, store=store)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """
        Called after the user finishes speaking, BEFORE the LLM generates.
        This is where we tag language, update the tracker, and update the
        instructions so the LLM sees the correct language context on THIS
        turn, producing an immediate response in the right language without
        a one-turn lag.
        """
        text = new_message.text_content or ""
        if not text.strip():
            return

        expected_epoch = self.orchestrator.turn_epoch

        lang, confidence = naive_language_tag(text)
        switch = self.tracker.ingest(
            text=text,
            lang=lang,
            confidence=confidence,
            start_ms=0,
            end_ms=len(text) * 60,  # placeholder timing; swap for real STT timestamps
        )
        if switch:
            logger.info(
                "Code-switch detected: %s -> %s on %r (conf=%.2f)",
                switch.from_lang, switch.to_lang, switch.trigger_segment, switch.confidence,
            )

        # Extract maintenance intake fields from caller speech
        extracted = extract_task_fields(text, self.task_state)
        if extracted:
            self.task_state.update(extracted)
            logger.info("Task state updated: %s -> full task_state=%s", extracted, self.task_state)

        # Update instructions NOW — the LLM hasn't sent its first token yet.
        plan = self.tracker.response_language_plan()
        new_instructions = build_system_prompt(plan, self.task_state)
        await self.update_instructions(new_instructions)

        # State Fencing: Persist state guarded by expected_epoch.
        # If an interruption occurred while processing, obsolete state is dropped.
        saved = self.orchestrator.fenced_save_session(
            self.phone_number,
            self.call_sid,
            self.task_state,
            self.tracker.state.to_dict(),
            expected_epoch=expected_epoch,
        )
        if saved:
            logger.debug("Turn state durably persisted at epoch %d", expected_epoch)

    async def create_maintenance_ticket(self, unit: str, category: str, description: str) -> str:
        """
        Example asynchronous tool call guarded by state fencing.
        If the user interrupts while the ticket is being created, the task is
        cancelled and no partial or obsolete ticket is committed to state.
        """
        async def _do_create_ticket():
            await asyncio.sleep(0.05)  # simulate API / DB write
            ticket_id = f"TICK-{int(time.time())}"
            logger.info("Ticket %s created for unit %s", ticket_id, unit)
            return ticket_id

        ticket_id = await self.orchestrator.execute_fenced_tool(
            "create_maintenance_ticket", _do_create_ticket
        )
        self.task_state["ticket_id"] = ticket_id
        return ticket_id


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: JobContext):
    await ctx.connect()

    phone_number = ctx.room.name  # configure Twilio dispatch rule to set room name = caller number
    # LiveKit Playground / Cloud Console generates randomized room names (e.g. 'console-f9ef3afe').
    # Map console-* rooms to a shared test identity so dropped-call recovery works
    # seamlessly during browser playground demos and restarts!
    if phone_number.startswith("console-"):
        phone_number = "dev-caller-playground"

    call_sid = ctx.job.id

    recovered = store.load_or_start(phone_number, call_sid)
    task_state: dict = recovered.task_state
    tracker = LanguageTracker(
        state=LanguageState.from_dict(recovered.language_state_dict) if recovered.is_recovery else None
    )

    if recovered.is_recovery:
        logger.info(
            "Recovered session for %s (%.0fs since last activity). "
            "Resuming task_state=%s lang=%s",
            phone_number, recovered.seconds_since_drop, task_state, tracker.state.current,
        )

    primary_tts = create_rime_tts()
    fallback_tts = None  # wire a disclosed fallback provider here before production use
    speaker = RimeSpeaker(tts_session=primary_tts, fallback_tts_session=fallback_tts)

    # 1. Initialize Interruption Orchestrator for low-latency buffer cancellation & state fencing
    orchestrator = InterruptionOrchestrator(
        speaker=speaker,
        store=store,
    )

    agent = SangamAgent(
        tracker=tracker,
        task_state=task_state,
        phone_number=phone_number,
        call_sid=call_sid,
        speaker=speaker,
        orchestrator=orchestrator,
    )

    session = AgentSession()
    orchestrator.bind_session(session)

    # 2. Event-Driven Interruption: Hook into LiveKit VAD/turn-detection events
    # Event A: LiveKit AgentSession user_state_changed (emitted immediately on VAD speech onset)
    @session.on("user_state_changed")
    def on_user_state_changed(ev):
        if getattr(ev, "new_state", None) == "speaking":
            logger.info(
                "VAD event 'user_state_changed' -> speaking. Triggering sub-200ms buffer truncation."
            )
            asyncio.create_task(orchestrator.on_user_started_speaking(ev))

    # Event B: Explicit user_started_speaking event (for turn detector / direct VAD stream hooks)
    @session.on("user_started_speaking")
    def on_user_started_speaking(*args, **kwargs):
        logger.info(
            "Turn-detection event 'user_started_speaking' received. Truncating TTS stream & fencing state."
        )
        asyncio.create_task(orchestrator.on_user_started_speaking())

    await session.start(agent=agent, room=ctx.room)

    # Opening / recovery greeting — session.say() streams into the pipeline.
    if recovered.is_recovery:
        room = task_state.get("room_number", "")
        if tracker.state.dominant == Lang.HI:
            if room:
                resume_text = f"Welcome back जी! Room {room} की request वहीं से शुरू करते हैं। बताइए आगे क्या दिक्कत आ रही है?"
            else:
                resume_text = "Welcome back जी! आपकी maintenance request वहीं से शुरू करते हैं जहाँ हम रुके थे।"
        else:
            if room:
                resume_text = f"Welcome back! Let's continue with your maintenance request for room {room}. What issue are you experiencing?"
            else:
                resume_text = "Welcome back! Let's pick back up where we left off with your maintenance request."
        session.say(resume_text)
    else:
        session.say(
            "नमस्ते जी। अपना room या flat number बताइए।"
        )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
