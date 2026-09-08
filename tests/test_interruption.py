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
