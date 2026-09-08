"""
rime_client.py

Thin wrapper around LiveKit's Rime inference integration. Two things this
module exists to guarantee, both required by the hackathon rules:

1. The active speech provider is OBSERVABLE at runtime (not just in code) —
   `RimeSpeaker.active_provider` is surfaced to the eval harness and can be
   logged/printed live during the demo so judges can see Rime is actually
   the one talking.
2. Fallback is explicit and disclosed, not silent. If Rime errors, we fall
   back to a lower-quality provider ONLY so the call doesn't die, and we
   flag every fallback-spoken turn in the transcript log.

Model choice: Coda with the native Hindi voice, Taru. The old default,
Celeste, is catalogued as an American English voice; it can render Hindi
text, but it gives a noticeably non-local accent and uneven Hindi cadence.
LiveKit's current inference gateway routes this integration through Coda, so
the model and speaker must be a verified Coda Hindi pair. Do not send Rime
speed controls here: this gateway rejects `speedAlpha` for Coda.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("sangam.rime")

RIME_MODEL = "rime/coda"
DEFAULT_VOICE = "taru"
DEFAULT_LANGUAGE = "hi"
AUDIO_FORMAT = "pcm"
SAMPLE_RATE = 24000


def create_rime_tts():
    """Build the one Hindi-first Rime configuration used for every live turn."""
    # Keep this import local so configuration constants remain importable in
    # tests and smoke checks without constructing a LiveKit inference client.
    from livekit.agents import inference

    return inference.TTS(
        model=RIME_MODEL,
        voice=DEFAULT_VOICE,
        language=DEFAULT_LANGUAGE,
    )


@dataclass
class SpeakEvent:
    text: str
    provider: str
    model: str
    requested_at: float
    first_audio_at: float | None = None
    completed_at: float | None = None
    fell_back: bool = False

    @property
    def ttfb_ms(self) -> float | None:
        if self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.requested_at) * 1000


class RimeSpeaker:
    """
    Wraps LiveKit's `inference.TTS(model="rime/coda", ...)`. Kept as a
    thin adapter (rather than calling LiveKit's TTS object directly from
    main.py) so:
      - fallback logic lives in one place
      - every spoken turn is logged as a SpeakEvent for the eval harness
      - the active provider is queryable at any point during the call
    """

    def __init__(self, tts_session, fallback_tts_session=None):
        self.tts_session = tts_session
        self.fallback_tts_session = fallback_tts_session
        self.active_provider = "rime"
        self._log: list[SpeakEvent] = []
        self._cancel_requested: bool = False

    def cancel_active_synthesis(self) -> None:
        """Immediately signals the active Rime synthesis stream to abort and drop audio chunks."""
        self._cancel_requested = True
        logger.info("RimeSpeaker active synthesis cancelled (<200ms truncation).")

    async def speak(self, text: str, language_hint: str = DEFAULT_LANGUAGE) -> SpeakEvent:
        self._cancel_requested = False
        event = SpeakEvent(text=text, provider="rime", model=RIME_MODEL, requested_at=time.time())
        try:
            first_chunk = True
            async for _chunk in self.tts_session.synthesize(text):
                if self._cancel_requested:
                    logger.debug("Discarding remaining Rime audio chunk due to interruption.")
                    break
                if first_chunk:
                    event.first_audio_at = time.time()
                    first_chunk = False
            self.active_provider = "rime"
        except asyncio.CancelledError:
            logger.info("RimeSpeaker task cancelled via asyncio.")
            raise
        except Exception as exc:  # noqa: BLE001 — telephony errors are heterogeneous
            if self._cancel_requested:
                return event
            logger.warning("Rime synthesis failed (%s). Falling back.", exc)
            event.fell_back = True
            event.provider = "fallback"
            self.active_provider = "fallback"
            if self.fallback_tts_session is not None:
                first_chunk = True
                async for _chunk in self.fallback_tts_session.synthesize(text):
                    if self._cancel_requested:
                        break
                    if first_chunk:
                        event.first_audio_at = time.time()
                        first_chunk = False
            else:
                raise
        finally:
            event.completed_at = time.time()
            self._log.append(event)
        return event

    def transcript_log(self) -> list[dict]:
        return [
            {
                "text": e.text,
                "provider": e.provider,
                "fell_back": e.fell_back,
                "ttfb_ms": e.ttfb_ms,
            }
            for e in self._log
        ]
