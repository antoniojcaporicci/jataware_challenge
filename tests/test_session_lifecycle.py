from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import MagicMock

from nightwatch_worker.session_lifecycle import (
    SessionPhase,
    attempt_session_start,
    session_status_from_payload,
)


class TestSessionStatusFromPayload(unittest.TestCase):
    def test_running_variants(self) -> None:
        for status in ("running", "Running", "ACTIVE", "in_progress"):
            ph, raw = session_status_from_payload({"status": status})
            self.assertEqual(ph, SessionPhase.RUNNING, status)
            self.assertEqual(raw, status)

    def test_pending(self) -> None:
        ph, raw = session_status_from_payload({"session_status": "pending"})
        self.assertEqual(ph, SessionPhase.PENDING)
        self.assertEqual(raw, "pending")

    def test_terminal_ok(self) -> None:
        ph, _ = session_status_from_payload({"status": "finished"})
        self.assertEqual(ph, SessionPhase.TERMINAL_OK)

    def test_terminal_bad(self) -> None:
        ph, _ = session_status_from_payload({"state": "failed"})
        self.assertEqual(ph, SessionPhase.TERMINAL_BAD)

    def test_session_finished_flag(self) -> None:
        ph, raw = session_status_from_payload({"session_finished": True, "status": "done"})
        self.assertEqual(ph, SessionPhase.TERMINAL_OK)
        self.assertEqual(raw, "done")

    def test_unknown_status_token(self) -> None:
        ph, raw = session_status_from_payload({"status": "weird_future_state"})
        self.assertEqual(ph, SessionPhase.UNKNOWN)
        self.assertEqual(raw, "weird_future_state")

    def test_non_object(self) -> None:
        self.assertEqual(session_status_from_payload(None)[0], SessionPhase.UNKNOWN)
        self.assertEqual(session_status_from_payload([])[0], SessionPhase.UNKNOWN)

    def test_missing_status(self) -> None:
        self.assertEqual(
            session_status_from_payload({"session_id": "x"})[0],
            SessionPhase.UNKNOWN,
        )


class TestAttemptSessionStart(unittest.TestCase):
    def test_posts_empty_json_body(self) -> None:
        client = MagicMock()
        client.post_json.return_value = (200, {"ok": True})
        log = MagicMock()
        attempt_session_start(client, "s1", log)
        client.post_json.assert_called_once_with(
            "/sessions/s1/start",
            {},
            rate_limited=False,
        )

    def test_urlerror_does_not_raise(self) -> None:
        client = MagicMock()
        client.post_json.side_effect = urllib.error.URLError("x")
        log = MagicMock()
        attempt_session_start(client, "s1", log)
        log.warning.assert_called()


if __name__ == "__main__":
    unittest.main()
