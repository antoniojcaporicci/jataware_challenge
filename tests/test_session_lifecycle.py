from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from nightwatch_worker.session_lifecycle import (
    SessionPhase,
    attempt_session_start,
    create_session,
    session_status_from_payload,
    wait_until_session_running,
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


class TestCreateSession(unittest.TestCase):
    def test_returns_session_id(self) -> None:
        client = MagicMock()
        client.post_json.return_value = (
            200,
            {"api_version": "v2", "session_id": "sess_new"},
        )
        log = MagicMock()
        sid = create_session(client, log, session_mode="practice", scenario_type="x")
        self.assertEqual(sid, "sess_new")
        client.post_json.assert_called_once_with(
            "/sessions",
            {"session_mode": "practice", "scenario_type": "x"},
            rate_limited=False,
        )


class TestWaitUntilSessionRunningRecreate(unittest.TestCase):
    @patch("nightwatch_worker.session_lifecycle.time.sleep")
    def test_finished_recreates_then_running(self, _sleep: MagicMock) -> None:
        client = MagicMock()
        client.post_json.side_effect = [
            (200, {}),
            (200, {"session_id": "s2", "api_version": "v2"}),
            (200, {}),
        ]
        client.get_json.side_effect = [
            (200, {"status": "finished"}),
            (200, {"status": "running"}),
        ]
        log = MagicMock()
        log.extra = {"session_id": "s1"}
        sid = wait_until_session_running(
            client,
            "s1",
            log,
            poll_interval_sec=0.01,
            assume_active=False,
            skip_session_start=False,
            recreate_if_finished=True,
        )
        self.assertEqual(sid, "s2")
        self.assertEqual(log.extra["session_id"], "s2")


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
