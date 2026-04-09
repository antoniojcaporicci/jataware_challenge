from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _minimal_env(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {"PATH": os.environ.get("PATH", "")}
    base.update(overrides)
    return base


class TestWorkerCli(unittest.TestCase):
    """CLI behavior for ``python -m nightwatch_worker`` / ``worker.py``."""

    py = sys.executable

    def test_help_exits_zero(self) -> None:
        r = subprocess.run(
            [self.py, "-m", "nightwatch_worker", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env=_minimal_env(),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nightwatch", r.stdout.lower())
        self.assertIn("challenge_http_cli", r.stdout)

    def test_worker_py_help_matches(self) -> None:
        r = subprocess.run(
            [self.py, str(REPO_ROOT / "worker.py"), "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env=_minimal_env(),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("challenge_http_cli", r.stdout)

    def test_missing_session_id_exits_2(self) -> None:
        r = subprocess.run(
            [self.py, "-m", "nightwatch_worker", "--no-env-file"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env=_minimal_env(API_URL="https://example.invalid", API_TOKEN="x"),
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("session", r.stderr.lower())

    def test_missing_api_url_exits_2(self) -> None:
        r = subprocess.run(
            [
                self.py,
                "-m",
                "nightwatch_worker",
                "--no-env-file",
                "--session-id",
                "s1",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env=_minimal_env(API_TOKEN="x"),
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("API_URL", r.stderr)

    def test_missing_api_token_exits_2(self) -> None:
        r = subprocess.run(
            [
                self.py,
                "-m",
                "nightwatch_worker",
                "--no-env-file",
                "--session-id",
                "s1",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            env=_minimal_env(API_URL="https://example.invalid"),
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("API_TOKEN", r.stderr)

    def test_poll_interval_clamped_and_logged(self) -> None:
        proc = subprocess.Popen(
            [
                self.py,
                "-m",
                "nightwatch_worker",
                "--no-env-file",
                "--skip-verify",
                "--assume-session-active",
                "--skip-initial-catalog",
                "--session-id",
                "sess-clamp",
                "--poll-interval",
                "1.5",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_minimal_env(
                API_URL="https://example.invalid",
                API_TOKEN="tok",
            ),
        )
        try:
            time.sleep(0.4)
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=2)

        self.assertEqual(proc.returncode, 0, err + out)
        self.assertIn("poll-interval raised", err)
        self.assertIn("sess-clamp", out)
        self.assertIn("poll_interval=5.0", out)

    def test_startup_log_then_interrupt_exits_zero(self) -> None:
        proc = subprocess.Popen(
            [
                self.py,
                "-m",
                "nightwatch_worker",
                "--no-env-file",
                "--skip-verify",
                "--assume-session-active",
                "--skip-initial-catalog",
                "--session-id",
                "sess-run",
                "--poll-interval",
                "5",
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_minimal_env(
                API_URL="https://example.invalid",
                API_TOKEN="tok",
            ),
        )
        try:
            time.sleep(0.4)
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=2)

        self.assertEqual(proc.returncode, 0, err + out)
        self.assertIn("worker started", out)
        self.assertIn("catalog_loaded=False", out)
        self.assertIn("sess-run", out)
        self.assertIn("stopped by user", out)


if __name__ == "__main__":
    unittest.main()
