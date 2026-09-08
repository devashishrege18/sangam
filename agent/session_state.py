"""
session_state.py

Telephony drops calls. That's not an edge case, it's Tuesday. The stress
case Sangam is built to survive: a caller is mid-way through reporting a
maintenance issue, the call drops (cell signal, carrier handoff, whatever),
they call back within the recovery window, and the agent resumes in the
SAME language state and task state — it does not re-greet them in English
and ask them to start over.

This is intentionally boring, durable storage (SQLite) rather than an
in-memory dict, because "survives a process restart" is part of the claim,
not just "survives a WebSocket blip."

Keyed by caller phone number (from Twilio's `From`), not by call SID, since
the whole point is bridging across two different call SIDs (the dropped
call and the callback).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

RECOVERY_WINDOW_SECONDS = 180  # how long a callback is treated as "the same conversation"

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_sessions (
    phone_number TEXT PRIMARY KEY,
    task_state_json TEXT NOT NULL,
    language_state_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    last_call_sid TEXT
);
"""


@dataclass
class RecoveredSession:
    task_state: dict
    language_state_dict: dict
    seconds_since_drop: float
    is_recovery: bool


class SessionStore:
    def __init__(self, db_path: str = "vaiven_sessions.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def load_or_start(self, phone_number: str, call_sid: str) -> RecoveredSession:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT task_state_json, language_state_json, updated_at "
                "FROM call_sessions WHERE phone_number = ?",
                (phone_number,),
            ).fetchone()

        now = time.time()
        if row is None:
            return RecoveredSession(task_state={}, language_state_dict={}, seconds_since_drop=0.0, is_recovery=False)

        task_state_json, language_state_json, updated_at = row
        gap = now - updated_at
        if gap > RECOVERY_WINDOW_SECONDS:
            # Too long ago — treat as a fresh call, not a continuation.
            return RecoveredSession(task_state={}, language_state_dict={}, seconds_since_drop=gap, is_recovery=False)

        return RecoveredSession(
            task_state=json.loads(task_state_json),
            language_state_dict=json.loads(language_state_json),
            seconds_since_drop=gap,
            is_recovery=True,
        )

    def save(self, phone_number: str, call_sid: str, task_state: dict, language_state_dict: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO call_sessions (phone_number, task_state_json, language_state_json, updated_at, last_call_sid)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(phone_number) DO UPDATE SET
                    task_state_json = excluded.task_state_json,
                    language_state_json = excluded.language_state_json,
                    updated_at = excluded.updated_at,
                    last_call_sid = excluded.last_call_sid
                """,
                (phone_number, json.dumps(task_state), json.dumps(language_state_dict), time.time(), call_sid),
            )

    def clear(self, phone_number: str) -> None:
        """Call this when a maintenance request is completed/confirmed —
        don't resume a finished conversation on the next unrelated call."""
        with self._conn() as conn:
            conn.execute("DELETE FROM call_sessions WHERE phone_number = ?", (phone_number,))

