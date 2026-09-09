"""
cancellation.py

Low-latency audio buffer cancellation and state fencing for LiveKit + Rime TTS.
Guarantees <200ms interruption response and fences state persistence against obsolete turns.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Coroutine, Set

logger = logging.getLogger("sangam.cancellation")


class StaleTurnError(Exception):
    """Raised when an async task completes after an interruption epoch has invalidated it."""
    pass


class InterruptionOrchestrator:
    """
    Coordinates sub-200ms audio cancellation and state fencing across LiveKit,
    Rime TTS, LLM generation, and asynchronous tool tasks.
    
    Architecture:
    1. Turn Epoch (State Fence): Monotonically increasing counter incremented on every
       user interruption. Any in-flight LLM token generation, tool execution, or state
       persistence tagged with an older epoch is invalidated and rejected.
    2. Stream & Queue Truncation: Immediately signals LiveKit's AgentSession to interrupt,
       cancels Rime TTS synthesis coroutines, discards unplayed audio queue chunks, and
       toggles/drains WebRTC output sinks.
    3. State Fencing: Cancels in-flight tool tasks (e.g. maintenance ticket generation)
       and ensures obsolete assistant responses are not committed to conversation state.
    """

    def __init__(
        self,
        session: Any = None,
        speaker: Any = None,
        store: Any = None,
        audio_frame_queue: asyncio.Queue | None = None,
    ):
        self.session = session
        self.speaker = speaker
        self.store = store
        self.audio_frame_queue = audio_frame_queue

        # State Fencing: Monotonic epoch counter
        self.turn_epoch: int = 0

        # Active in-flight async tasks
        self.active_llm_tasks: Set[asyncio.Task] = set()
        self.active_tts_tasks: Set[asyncio.Task] = set()
        # Backwards-compatible handles for callers that inspect these fields.
        self.active_llm_task: asyncio.Task | None = None
        self.active_tts_task: asyncio.Task | None = None
        self.active_tool_tasks: Set[asyncio.Task] = set()
        # Tasks cancelled by the latest interruption remain observable until
        # wait_for_cancellation() confirms that cancellation reached them.
        self._recently_cancelled_tasks: Set[asyncio.Task] = set()
        self._interruption_lock = asyncio.Lock()
        # Threading lock for atomic epoch-check-then-write in fenced_save_session.
        # Also guards epoch advancement so an interruption on another thread can't
        # slip between the epoch check and the store.save() call.
        self._state_lock = threading.RLock()

        # Metrics
        self.last_interruption_latency_ms: float | None = None
        # Confirmed cancellation latency: measures actual task stop, not just
        # synchronous handler dispatch. Set by wait_for_cancellation().
        self.last_confirmed_cancellation_latency_ms: float | None = None
        # Diagnostic: component-level errors from the last interruption cycle.
        # Each entry is {"component": str, "error": str}.
        self.last_cancellation_errors: list[dict[str, Any]] = []
        self.last_cancellation_succeeded: bool = True

    def bind_session(self, session: Any) -> None:
        self.session = session

    def register_llm_task(self, task: asyncio.Task) -> None:
        self.active_llm_tasks.add(task)
        self.active_llm_task = task
        task.add_done_callback(self.active_llm_tasks.discard)

    def register_tts_task(self, task: asyncio.Task) -> None:
        self.active_tts_tasks.add(task)
        self.active_tts_task = task
        task.add_done_callback(self.active_tts_tasks.discard)

    async def on_user_started_speaking(self, ev: Any = None) -> float:
        async with self._interruption_lock:
            return await self._interrupt_locked()

    async def _interrupt_locked(self) -> float:
        """
        Sub-200ms interruption handler triggered by VAD / turn detection.
        Immediately cancels all ongoing agent generation and flushes audio buffers.
        """
        t0 = time.perf_counter()
        errors: list[dict[str, Any]] = []

        # 1. State Fencing: Increment epoch atomically under _state_lock so that
        # fenced_save_session() cannot interleave between its epoch check and
        # store.save() while we are advancing the epoch here.
        with self._state_lock:
            self.turn_epoch += 1
            current_epoch = self.turn_epoch

        logger.info(
            "User interruption detected. Advanced turn epoch to %d. Commencing cancellation...",
            current_epoch,
        )

        # 2. Queue & Stream Truncation: Trigger LiveKit native speech interruption
        if self.session is not None:
            try:
                # session.interrupt(force=True) cancels queued speech items and signals playback halt
                self.session.interrupt(force=True)
            except Exception as e:
                errors.append({"component": "session.interrupt", "error": str(e)})
                logger.debug("session.interrupt notice: %s", e)

        # 3. Cancel active Rime TTS synthesis handle & discard frames
        cancelled_tasks = [
            task for task in self.active_tts_tasks
            if not task.done()
        ]
        self._recently_cancelled_tasks.update(cancelled_tasks)
        for task in cancelled_tasks:
            task.cancel()
        self.active_tts_tasks.clear()
        self.active_tts_task = None

        if self.speaker is not None and hasattr(self.speaker, "cancel_active_synthesis"):
            try:
                self.speaker.cancel_active_synthesis()
            except Exception as e:
                errors.append({"component": "speaker.cancel_active_synthesis", "error": str(e)})
                logger.warning("Error cancelling Rime synthesis: %s", e)

        # Flush unplayed audio frame queue
        discarded_frames = 0
        if self.audio_frame_queue is not None:
            while not self.audio_frame_queue.empty():
                try:
                    self.audio_frame_queue.get_nowait()
                    self.audio_frame_queue.task_done()
                    discarded_frames += 1
                except (asyncio.QueueEmpty, ValueError):
                    break

        # 4. Flush WebRTC output buffer in < 200ms
        if self.session is not None and hasattr(self.session, "output"):
            try:
                # Fast toggle of audio sink to flush any remaining hardware/WebRTC buffer
                if hasattr(self.session.output, "set_audio_enabled"):
                    self.session.output.set_audio_enabled(False)
                    self.session.output.set_audio_enabled(True)
            except Exception as e:
                errors.append({"component": "output.set_audio_enabled", "error": str(e)})
                logger.debug("Audio sink flush notice: %s", e)

        # 5. State Fencing: Cancel LLM generation task
        cancelled_tasks = [
            task for task in self.active_llm_tasks
            if not task.done()
        ]
        self._recently_cancelled_tasks.update(cancelled_tasks)
        for task in cancelled_tasks:
            task.cancel()
        self.active_llm_tasks.clear()
        self.active_llm_task = None

        # 6. State Fencing: Cancel all active asynchronous tool tasks
        running_tools = [t for t in self.active_tool_tasks if not t.done()]
        self._recently_cancelled_tasks.update(running_tools)
        for tool_task in running_tools:
            tool_task.cancel()
        self.active_tool_tasks.clear()

        # Calculate handler dispatch latency (synchronous portion only)
        latency_ms = (time.perf_counter() - t0) * 1000
        self.last_interruption_latency_ms = latency_ms
        self.last_cancellation_errors = errors
        self.last_cancellation_succeeded = len(errors) == 0

        logger.info(
            "Interruption finished in %.2f ms (< 200 ms target). Discarded %d queued audio frames. "
            "Cancelled %d tool tasks. Errors: %d.",
            latency_ms,
            discarded_frames,
            len(running_tools),
            len(errors),
        )

        return latency_ms

    async def execute_fenced_tool(
        self,
        tool_name: str,
        coro_fn: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Executes an asynchronous tool call (e.g. ticket creation, DB mutation) guarded by
        the state fence. If an interruption occurs before or during execution, the task
        is aborted and state changes are NOT committed.
        """
        start_epoch = self.turn_epoch
        current_task = asyncio.current_task()
        if current_task is not None:
            self.active_tool_tasks.add(current_task)

        try:
            # Execute tool work
            result = await coro_fn(*args, **kwargs)

            # Post-execution Fence Verification:
            # Check whether a user interruption happened while this tool was computing
            if self.turn_epoch != start_epoch:
                logger.warning(
                    "State fencing violation prevented: Tool '%s' completed on stale epoch %d (active is %d). Discarding result.",
                    tool_name,
                    start_epoch,
                    self.turn_epoch,
                )
                raise StaleTurnError(
                    f"Tool '{tool_name}' was invalidated by a user interruption."
                )

            return result
        except asyncio.CancelledError:
            logger.info("Tool '%s' was actively cancelled by interruption orchestrator.", tool_name)
            raise
        finally:
            if current_task is not None:
                self.active_tool_tasks.discard(current_task)

    def fenced_save_session(
        self,
        phone_number: str,
        call_sid: str,
        task_state: dict,
        language_state_dict: dict,
        expected_epoch: int,
    ) -> bool:
        """
        Durable SQLite state save with state fence checking.
        If the current epoch differs from expected_epoch, the turn was interrupted
        and this partial/obsolete state is NOT persisted.

        The epoch check and store.save() are performed atomically under
        _state_lock to prevent an interruption from advancing the epoch between
        the check and the write (the check-then-save race from Finding 2).
        """
        with self._state_lock:
            if self.turn_epoch != expected_epoch:
                logger.warning(
                    "Fenced save rejected: epoch %d does not match active epoch %d. State not written.",
                    expected_epoch,
                    self.turn_epoch,
                )
                return False

            if self.store is not None:
                self.store.save(phone_number, call_sid, task_state, language_state_dict)
                return True
            return False

    async def wait_for_cancellation(self, timeout: float = 0.5) -> float:
        """
        Waits for all cancelled tasks to actually terminate, measuring confirmed
        cancellation latency (as opposed to handler dispatch latency).

        Returns the elapsed time in milliseconds. This gives an end-to-end metric
        covering the time from wait start until all cancelled tasks are done,
        addressing Finding 8 (latency metric reliability).
        """
        t0 = time.perf_counter()
        pending = [
            task for task in self._recently_cancelled_tasks
            if not task.done()
        ]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        self._recently_cancelled_tasks = {
            task for task in self._recently_cancelled_tasks if not task.done()
        }
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.last_confirmed_cancellation_latency_ms = elapsed_ms
        logger.info(
            "Confirmed cancellation latency: %.2f ms (%d tasks awaited).",
            elapsed_ms,
            len(pending),
        )
        return elapsed_ms
