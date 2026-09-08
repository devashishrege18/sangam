"""
language_tracker.py

The hard problem Sangam exists to solve: real Hindi-English (Hinglish)
speakers don't pick a language and stay in it. They code-switch
mid-sentence, mid-clause, even mid-word ("bhai, ye leak abhi tak fix nahi
hua", "the landlord ne bola ki kal aayega"). Most voice agents treat
language as a call-level setting chosen once at the start (an IVR menu in
disguise — "Press 2 for Hindi"). That fails the moment a caller mixes
languages, which is the normal way most urban Indian callers actually
speak — Hinglish isn't a corner case, it's the default register.

This module tracks language at the SEGMENT level (clause-sized spans from
STT, not full utterances), keeps a rolling estimate of the caller's dominant
language, and decides whether the agent's next response should mirror the
caller's switch or hold steady — because always mirroring is jarring and
never mirroring erases the caller's own communication choice.

Design decisions worth defending in the demo:
- We track a LANGUAGE STATE, not a language DECISION. State persists across
  turns and across reconnects (see session_state.py) so a dropped call that
  reconnects mid-sentence doesn't reset to a language menu.
- Switch detection requires two consecutive segments in the new language
  above a confidence floor, to avoid flapping on STT misfires on short
  words ("nahi", "haan", "OK") that are ambiguous between languages —
  Hinglish is full of these short particles and discourse markers.
- We record every switch as an event with a timestamp and confidence, so
  the evidence harness can replay and grade decisions after the fact —
  this is what makes the "hard voice problem" claim falsifiable rather
  than anecdotal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Lang(str, Enum):
    EN = "en"
    HI = "hi"
    UNKNOWN = "unk"


@dataclass
class Segment:
    """A single clause-level chunk from STT, already language-tagged."""
    text: str
    lang: Lang
    confidence: float
    start_ms: int
    end_ms: int


@dataclass
class SwitchEvent:
    from_lang: Lang
    to_lang: Lang
    at_ms: int
    trigger_segment: str
    confidence: float


@dataclass
class LanguageState:
    """Persisted per call. Serializable so it survives a reconnect."""
    dominant: Lang = Lang.UNKNOWN
    current: Lang = Lang.UNKNOWN
    history: list[Segment] = field(default_factory=list)
    switches: list[SwitchEvent] = field(default_factory=list)
    # Counts used for dominant-language voting; decays so a call that
    # starts English-heavy but drifts to Hindi updates its estimate.
    _lang_weight: dict[str, float] = field(default_factory=lambda: {"en": 0.0, "hi": 0.0})

    def to_dict(self) -> dict:
        return {
            "dominant": self.dominant.value,
            "current": self.current.value,
            "history": [
                {"text": s.text, "lang": s.lang.value, "confidence": s.confidence,
                 "start_ms": s.start_ms, "end_ms": s.end_ms}
                for s in self.history
            ],
            "switches": [
                {"from_lang": sw.from_lang.value, "to_lang": sw.to_lang.value,
                 "at_ms": sw.at_ms, "trigger_segment": sw.trigger_segment,
                 "confidence": sw.confidence}
                for sw in self.switches
            ],
            "_lang_weight": self._lang_weight,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LanguageState":
        state = cls(
            dominant=Lang(d.get("dominant", "unk")),
            current=Lang(d.get("current", "unk")),
            _lang_weight=d.get("_lang_weight", {"en": 0.0, "hi": 0.0}),
        )
        state.history = [
            Segment(s["text"], Lang(s["lang"]), s["confidence"], s["start_ms"], s["end_ms"])
            for s in d.get("history", [])
        ]
        state.switches = [
            SwitchEvent(Lang(sw["from_lang"]), Lang(sw["to_lang"]), sw["at_ms"],
                        sw["trigger_segment"], sw["confidence"])
            for sw in d.get("switches", [])
        ]
        return state


class LanguageTracker:
    """
    Feed it STT segments as they arrive. It maintains LanguageState and
    tells the orchestrator (main.py) what language context to hand the
    LLM and Rime for the next response.

    CONFIRM_STREAK: number of consecutive same-language segments required
    before we treat it as a real switch rather than STT noise. Tunable —
    documented in RIME_EVIDENCE.md with the fixture results that justified
    the chosen value.
    """

    CONFIRM_STREAK = 2
    MIN_SWITCH_CONFIDENCE = 0.55
    # Segments at or above this confidence commit a switch immediately
    # (no streak required). This was added after eval fixture cs-03 failed:
    # genuine rapid Hinglish alternation ("paani ki fuga hai" / "in the
    # bathroom" / "bahut urgent hai") never produces two consecutive
    # same-language segments, so a streak requirement misses it entirely.
    # High-confidence STT output is reliable enough in our fixture testing
    # to commit on one segment; the streak requirement is reserved for the
    # ambiguous, lower-confidence band where single-word misfires
    # ("nahi", "haan", short particles) are the real risk (see cs-04).
    HIGH_CONFIDENCE_COMMIT = 0.75
    DECAY = 0.9  # older segments count less toward "dominant" language

    def __init__(self, state: LanguageState | None = None):
        self.state = state or LanguageState()
        self._pending_lang: Lang | None = None
        self._pending_streak = 0

    def ingest(self, text: str, lang: Lang, confidence: float,
               start_ms: int, end_ms: int) -> SwitchEvent | None:
        seg = Segment(text, lang, confidence, start_ms, end_ms)
        self.state.history.append(seg)
        self._update_dominant(seg)

        if lang == Lang.UNKNOWN or confidence < self.MIN_SWITCH_CONFIDENCE:
            # Low-confidence segment: update history for the record, but
            # don't let it drive a switch decision.
            return None

        if self.state.current == Lang.UNKNOWN:
            self.state.current = lang
            self._pending_lang, self._pending_streak = None, 0
            return None

        if lang == self.state.current:
            self._pending_lang, self._pending_streak = None, 0
            return None

        # High-confidence segments commit a switch immediately — see
        # HIGH_CONFIDENCE_COMMIT docstring above for why.
        if confidence >= self.HIGH_CONFIDENCE_COMMIT:
            event = SwitchEvent(
                from_lang=self.state.current,
                to_lang=lang,
                at_ms=start_ms,
                trigger_segment=text,
                confidence=confidence,
            )
            self.state.switches.append(event)
            self.state.current = lang
            self._pending_lang, self._pending_streak = None, 0
            return event

        # Lower-confidence candidate switch — require a confirm streak.
        if lang == self._pending_lang:
            self._pending_streak += 1
        else:
            self._pending_lang, self._pending_streak = lang, 1

        if self._pending_streak >= self.CONFIRM_STREAK:
            event = SwitchEvent(
                from_lang=self.state.current,
                to_lang=lang,
                at_ms=start_ms,
                trigger_segment=text,
                confidence=confidence,
            )
            self.state.switches.append(event)
            self.state.current = lang
            self._pending_lang, self._pending_streak = None, 0
            return event

        return None

    def _update_dominant(self, seg: Segment) -> None:
        if seg.lang == Lang.UNKNOWN:
            return
        for k in self.state._lang_weight:
            self.state._lang_weight[k] *= self.DECAY
        self.state._lang_weight[seg.lang.value] += seg.confidence
        self.state.dominant = (
            Lang.EN if self.state._lang_weight["en"] >= self.state._lang_weight["hi"]
            else Lang.HI
        )

    def response_language_plan(self) -> dict:
        """
        Called right before generating a response. Returns guidance for
        prompts.py: whether to mirror the caller's current language, hold
        the dominant language, and whether the turn is itself a
        code-switch boundary (which changes how Rime should render it —
        see prompts.py MIRROR_SWITCH_INSTRUCTION).
        """
        recent_switch = bool(
            self.state.switches and
            self.state.history and
            self.state.history[-1].end_ms - self.state.switches[-1].at_ms < 4000
        )
        return {
            "current": self.state.current.value,
            "dominant": self.state.dominant.value,
            "recent_switch": recent_switch,
            "mirror": True if recent_switch else self.state.current == self.state.dominant,
        }

