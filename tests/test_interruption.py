"""
tests/test_interruption.py

Unit tests for low-latency audio buffer cancellation and state fencing.
Validates:
1. Event-driven interruption via user_started_speaking / user_state_changed
2. Stream & queue truncation (<200ms)
3. State fencing: active task cancellation, rejection of obsolete tool mutations,
   and prevention of state persistence on interrupted turns.
"""

from __future__ import annotations

import asyncio
import time
import pytest

from agent.cancellation import InterruptionOrchestrator, StaleTurnError
from agent.session_state import SessionStore


class MockSession:
    def __init__(self):
        self.interrupted = False
        self.interrupt_force = False
        self.audio_enabled_toggles = []

        class MockOutput:
            def __init__(self, parent):
                self.parent = parent
            def set_audio_enabled(self, enabled: bool):
                self.parent.audio_enabled_toggles.append(enabled)

        self.output = MockOutput(self)

    def interrupt(self, *, force: bool = False):
        self.interrupted = True
        self.interrupt_force = force


class MockSpeaker:
    def __init__(self):
        self.cancel_called = False

    def cancel_active_synthesis(self):
        self.cancel_called = True


@pytest.mark.anyio
async def test_low_latency_truncation_under_200ms():
    """Verify that buffer cancellation and task teardown executes well under 200ms."""
    session = MockSession()
    speaker = MockSpeaker()
    queue = asyncio.Queue()
    for i in range(20):
        await queue.put(f"pcm_chunk_{i}")

    orchestrator = InterruptionOrchestrator(
        session=session,
        speaker=speaker,
        audio_frame_queue=queue,
    )

    # Simulate an active LLM generation task and TTS task
    async def dummy_llm():
        await asyncio.sleep(10)

    async def dummy_tts():
        await asyncio.sleep(10)

    llm_task = asyncio.create_task(dummy_llm())
    tts_task = asyncio.create_task(dummy_tts())
    orchestrator.register_llm_task(llm_task)
    orchestrator.register_tts_task(tts_task)

    # Trigger interruption
    latency_ms = await orchestrator.on_user_started_speaking()

    # Yield briefly so the event loop delivers CancelledError to tasks
    await asyncio.sleep(0)

    # Latency must be < 200ms (typically < 10ms in local event loop)
    assert latency_ms < 200.0
    assert session.interrupted is True
    assert session.interrupt_force is True
    assert speaker.cancel_called is True
    assert queue.empty() is True
    assert llm_task.cancelled() is True
    assert tts_task.cancelled() is True
    assert session.audio_enabled_toggles == [False, True]


@pytest.mark.anyio
async def test_state_fencing_prevents_stale_tool_commit(tmp_path):
    """Verify that a tool executing during an interruption cannot commit stale state."""
    db_file = str(tmp_path / "test_fence.db")
    store = SessionStore(db_path=db_file)
    orchestrator = InterruptionOrchestrator(store=store)

    ticket_committed = False

    async def slow_ticket_creation():
        nonlocal ticket_committed
        await asyncio.sleep(0.1)  # Simulate API delay
        ticket_committed = True
        return "TICKET-123"

    # Start tool in background
    tool_task = asyncio.create_task(
        orchestrator.execute_fenced_tool("create_ticket", slow_ticket_creation)
    )

    # User interrupts 20ms into ticket creation
    await asyncio.sleep(0.02)
    await orchestrator.on_user_started_speaking()

    # Tool task should have been cancelled by the orchestrator
    with pytest.raises(asyncio.CancelledError):
        await tool_task

    assert ticket_committed is False


@pytest.mark.anyio
async def test_state_fencing_rejects_obsolete_epoch_persistence(tmp_path):
    """Verify that store.save() is rejected if the turn epoch has advanced."""
    db_file = str(tmp_path / "test_save_fence.db")
    store = SessionStore(db_path=db_file)
    orchestrator = InterruptionOrchestrator(store=store)

    caller_phone = "+15551234567"
    call_sid = "CALL-001"

    # Turn starts at epoch 0
    start_epoch = orchestrator.turn_epoch

    # Interruption happens before save
    await orchestrator.on_user_started_speaking()

    # Attempt to persist state using the old epoch
    success = orchestrator.fenced_save_session(
        phone_number=caller_phone,
        call_sid=call_sid,
        task_state={"room_number": "402"},
        language_state_dict={"dominant": "hi"},
        expected_epoch=start_epoch,
    )

    assert success is False
    # Verify nothing was saved to DB
    recovered = store.load_or_start(caller_phone, call_sid)
    assert recovered.is_recovery is False


@pytest.mark.anyio
async def test_queue_join_does_not_hang_after_flush():
    """Finding 4: queue.join() must complete immediately after interruption flush
    because task_done() is called for every drained item."""
    queue = asyncio.Queue()
    for i in range(10):
        await queue.put(f"chunk_{i}")

    orchestrator = InterruptionOrchestrator(audio_frame_queue=queue)
    await orchestrator.on_user_started_speaking()

    assert queue.empty()
    # join() should return instantly — if task_done() was missed, this would hang
    await asyncio.wait_for(queue.join(), timeout=0.5)


@pytest.mark.anyio
async def test_multiple_llm_tts_tasks_all_cancelled():
    """Finding 5: registering multiple LLM/TTS tasks and interrupting should cancel ALL of them."""
    orchestrator = InterruptionOrchestrator()

    async def slow():
        await asyncio.sleep(10)

    llm1 = asyncio.create_task(slow())
    llm2 = asyncio.create_task(slow())
    tts1 = asyncio.create_task(slow())
    tts2 = asyncio.create_task(slow())

    orchestrator.register_llm_task(llm1)
    orchestrator.register_llm_task(llm2)
    orchestrator.register_tts_task(tts1)
    orchestrator.register_tts_task(tts2)

    await orchestrator.on_user_started_speaking()
    await asyncio.sleep(0)  # let CancelledError propagate

    assert llm1.cancelled()
    assert llm2.cancelled()
    assert tts1.cancelled()
    assert tts2.cancelled()


@pytest.mark.anyio
async def test_concurrent_interruptions_coalesce():
    """Finding 3: two simultaneous on_user_started_speaking calls should serialize
    and each advance the epoch exactly once (not race/corrupt)."""
    orchestrator = InterruptionOrchestrator()
    initial_epoch = orchestrator.turn_epoch

    # Fire two interruptions concurrently
    results = await asyncio.gather(
        orchestrator.on_user_started_speaking(),
        orchestrator.on_user_started_speaking(),
    )

    # Both returned latencies (no crash), and epoch advanced exactly twice
    assert orchestrator.turn_epoch == initial_epoch + 2
    assert all(isinstance(r, float) for r in results)


@pytest.mark.anyio
async def test_cancellation_error_collection():
    """Finding 7: component-level errors during interruption should be recorded."""

    class FailingSession:
        def interrupt(self, *, force=False):
            raise RuntimeError("session broken")

        class output:
            @staticmethod
            def set_audio_enabled(enabled):
                raise RuntimeError("audio sink broken")

    class FailingSpeaker:
        def cancel_active_synthesis(self):
            raise RuntimeError("speaker broken")

    orchestrator = InterruptionOrchestrator(
        session=FailingSession(),
        speaker=FailingSpeaker(),
    )
    await orchestrator.on_user_started_speaking()

    assert orchestrator.last_cancellation_succeeded is False
    assert len(orchestrator.last_cancellation_errors) >= 2
    components = [e["component"] for e in orchestrator.last_cancellation_errors]
    assert "session.interrupt" in components
    assert "speaker.cancel_active_synthesis" in components


@pytest.mark.anyio
async def test_confirmed_cancellation_latency():
    """Finding 8: wait_for_cancellation() should measure actual task stop latency."""
    orchestrator = InterruptionOrchestrator()

    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    orchestrator.register_llm_task(task)

    await orchestrator.on_user_started_speaking()
    confirmed_ms = await orchestrator.wait_for_cancellation(timeout=1.0)

    assert confirmed_ms is not None
    assert orchestrator.last_confirmed_cancellation_latency_ms == confirmed_ms
    assert task.cancelled()


@pytest.mark.anyio
async def test_atomic_fenced_save_under_lock(tmp_path):
    """Finding 2: epoch check and store.save() happen atomically under _state_lock,
    so an interruption cannot slip between them."""
    db_file = str(tmp_path / "test_atomic.db")
    store = SessionStore(db_path=db_file)
    orchestrator = InterruptionOrchestrator(store=store)

    caller = "+15559999999"
    sid = "CALL-ATOMIC"

    # Save at current epoch should succeed
    epoch = orchestrator.turn_epoch
    success = orchestrator.fenced_save_session(
        phone_number=caller,
        call_sid=sid,
        task_state={"room_number": "101"},
        language_state_dict={"dominant": "en"},
        expected_epoch=epoch,
    )
    assert success is True

    # Verify data was saved
    recovered = store.load_or_start(caller, sid)
    assert recovered.is_recovery is True
    assert recovered.task_state["room_number"] == "101"

    # Interrupt, then try saving with stale epoch
    await orchestrator.on_user_started_speaking()
    stale_success = orchestrator.fenced_save_session(
        phone_number=caller,
        call_sid=sid,
        task_state={"room_number": "999"},
        language_state_dict={"dominant": "hi"},
        expected_epoch=epoch,
    )
    assert stale_success is False

    # Data should still be the old save, not overwritten
    recovered2 = store.load_or_start(caller, "CALL-NEW")
    assert recovered2.task_state["room_number"] == "101"
