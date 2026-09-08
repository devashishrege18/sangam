"""
Pytest version of the recovery smoke test run manually during development.
Run with: pytest tests/
"""

import os
import time

import pytest

from agent.session_state import SessionStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_vaiven.db")
    return SessionStore(db_path=db_path)


def test_first_call_is_not_a_recovery(store):
    result = store.load_or_start("+15551234567", "CA_first")
    assert result.is_recovery is False
    assert result.task_state == {}


def test_dropped_call_recovers_state(store):
    store.load_or_start("+15551234567", "CA_first")
    task_state = {"unit_number": "402", "issue_category": "plumbing"}
    lang_state = {"dominant": "es", "current": "es", "history": [], "switches": [],
                  "_lang_weight": {"en": 0.1, "es": 2.0}}
    store.save("+15551234567", "CA_first", task_state, lang_state)

    time.sleep(0.5)
    result = store.load_or_start("+15551234567", "CA_second")
    assert result.is_recovery is True
    assert result.task_state["unit_number"] == "402"
    assert result.language_state_dict["current"] == "es"


def test_different_caller_does_not_inherit_state(store):
    store.save("+15551234567", "CA_first", {"unit_number": "402"}, {"current": "es"})
    result = store.load_or_start("+15559999999", "CA_other")
    assert result.is_recovery is False
    assert result.task_state == {}


def test_clear_removes_completed_session(store):
    store.save("+15551234567", "CA_first", {"unit_number": "402"}, {"current": "es"})
    store.clear("+15551234567")
    result = store.load_or_start("+15551234567", "CA_new")
    assert result.is_recovery is False

