from __future__ import annotations

import email
import http.client
import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

from nightwatch_worker.http_client import (
    ApiClient,
    INCIDENT_API_RATE_KEY,
    MIN_INCIDENT_ENDPOINT_INTERVAL_SEC,
    RateGate,
    incident_rate_family,
)


class _FakeCM:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.headers = http.client.HTTPMessage()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeCM:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _http_error(
    code: int,
    body: bytes = b"{}",
    *,
    retry_after: str | None = None,
) -> urllib.error.HTTPError:
    if retry_after is not None:
        hdrs = email.message_from_string(f"Retry-After: {retry_after}\n\n")
    else:
        hdrs = http.client.HTTPMessage()
    return urllib.error.HTTPError(
        url="http://example.invalid/",
        code=code,
        msg="err",
        hdrs=hdrs,
        fp=BytesIO(body),
    )


class TestIncidentRateFamily(unittest.TestCase):
    def test_incident_endpoints_mapped(self) -> None:
        for method, path in (
            ("GET", "/sessions/s1/incidents"),
            ("get", "/sessions/s1/incidents/i1"),
            ("GET", "/sessions/s1/incidents/i1/events"),
            ("POST", "/sessions/s1/incidents/i1/action"),
        ):
            self.assertEqual(
                incident_rate_family(method, path),
                INCIDENT_API_RATE_KEY,
                msg=f"{method} {path}",
            )

    def test_non_incident_paths_not_gated(self) -> None:
        self.assertIsNone(incident_rate_family("GET", "/sessions/s1/catalog"))
        self.assertIsNone(incident_rate_family("GET", "/auth/verify"))
        self.assertIsNone(incident_rate_family("GET", "/sessions/s1/incidents/i1/extra/foo"))

    def test_query_string_stripped_for_family(self) -> None:
        self.assertEqual(
            incident_rate_family("GET", "/sessions/s1/incidents?x=1"),
            INCIDENT_API_RATE_KEY,
        )


class TestRateGate(unittest.TestCase):
    def test_first_use_has_no_wait(self) -> None:
        g = RateGate(interval_sec=5.0)
        self.assertEqual(g.seconds_until_allowed("k"), 0.0)

    @patch("nightwatch_worker.http_client.time.sleep")
    @patch("nightwatch_worker.http_client.time.monotonic", side_effect=[0.0, 0.0, 5.0])
    def test_second_use_sleeps_for_remainder(
        self, _mono: MagicMock, sleep: MagicMock
    ) -> None:
        g = RateGate(interval_sec=5.0)
        g.wait_until_allowed("k")
        g.wait_until_allowed("k")
        sleep.assert_called_once_with(5.0)


class TestApiClient(unittest.TestCase):
    def _client_with_opener(self, opener: MagicMock) -> ApiClient:
        return ApiClient(
            "https://api.example",
            "secret-token",
            opener=opener,
        )

    def test_get_adds_bearer_and_returns_json(self) -> None:
        opener = MagicMock()
        opener.open.return_value = _FakeCM(200, b'{"api_version":"v2"}')

        client = self._client_with_opener(opener)
        status, data = client.get_json(
            "/sessions/s1/catalog",
            rate_limited=False,
        )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"api_version": "v2"})
        req = opener.open.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")
        self.assertTrue(str(req.full_url).startswith("https://api.example/sessions/s1/catalog"))

    def test_post_json_body_and_content_type(self) -> None:
        opener = MagicMock()
        opener.open.return_value = _FakeCM(
            200,
            json.dumps({"ok": True}).encode(),
        )

        client = self._client_with_opener(opener)
        status, data = client.post_json(
            "/sessions/s1/incidents/i1/action",
            {"action_id": "foo"},
            rate_limited=False,
        )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True})
        req = opener.open.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(req.data.decode()),
            {"action_id": "foo"},
        )

    def test_get_404_not_retried(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = _http_error(404, b"{}")
        client = self._client_with_opener(opener)
        status, _ = client.get_json(
            "/sessions/s1/catalog",
            rate_limited=False,
            retry_5xx_attempts=3,
        )
        self.assertEqual(status, 404)
        self.assertEqual(opener.open.call_count, 1)

    def test_get_503_retried_then_success(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = [
            _http_error(503, b"{}"),
            _FakeCM(200, b'{"r":1}'),
        ]
        client = self._client_with_opener(opener)
        status, data = client.get_json(
            "/sessions/s1/catalog",
            rate_limited=False,
            retry_5xx_attempts=2,
            retry_backoff_sec=0.01,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"r": 1})
        self.assertEqual(opener.open.call_count, 2)

    @patch("nightwatch_worker.http_client.time.sleep")
    def test_post_500_not_retried(self, _sleep: MagicMock) -> None:
        opener = MagicMock()
        opener.open.side_effect = _http_error(500, b"{}")
        client = self._client_with_opener(opener)
        status, _ = client.post_json(
            "/sessions/s1/incidents/i1/action",
            {"action_id": "x"},
            rate_limited=False,
        )
        self.assertEqual(status, 500)
        self.assertEqual(opener.open.call_count, 1)

    @patch("nightwatch_worker.http_client.time.sleep")
    def test_get_urlerror_retried(self, _sleep: MagicMock) -> None:
        opener = MagicMock()
        opener.open.side_effect = [
            urllib.error.URLError("broken"),
            _FakeCM(200, b"{}"),
        ]
        client = self._client_with_opener(opener)
        status, data = client.get_json(
            "/sessions/s1/catalog",
            rate_limited=False,
            retry_network_attempts=2,
            retry_backoff_sec=0.01,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {})
        self.assertEqual(opener.open.call_count, 2)

    @patch("nightwatch_worker.http_client.time.sleep")
    def test_incident_list_then_detail_shares_gate(self, sleep: MagicMock) -> None:
        opener = MagicMock()
        opener.open.return_value = _FakeCM(200, b"[]")

        client = ApiClient(
            "https://api.example",
            "t",
            min_incident_interval_sec=MIN_INCIDENT_ENDPOINT_INTERVAL_SEC,
            opener=opener,
        )
        client.get_json("/sessions/s1/incidents")
        client.get_json("/sessions/s1/incidents/i1")
        sleep.assert_called_once()
        self.assertGreaterEqual(
            sleep.call_args[0][0],
            MIN_INCIDENT_ENDPOINT_INTERVAL_SEC - 0.05,
        )

    @patch("nightwatch_worker.http_client.time.sleep")
    def test_get_429_retried_after_retry_after(self, sleep: MagicMock) -> None:
        opener = MagicMock()
        opener.open.side_effect = [
            _http_error(429, b"{}", retry_after="1"),
            _FakeCM(200, b"{}"),
        ]
        client = self._client_with_opener(opener)
        status, data = client.get_json(
            "/sessions/s1/catalog",
            rate_limited=False,
            retry_429_attempts=2,
            retry_backoff_sec=0.01,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {})
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once()
        self.assertGreaterEqual(sleep.call_args[0][0], 1.0)

    @patch("nightwatch_worker.http_client.time.sleep")
    def test_incident_get_waits_at_least_min_interval(self, sleep: MagicMock) -> None:
        opener = MagicMock()
        opener.open.return_value = _FakeCM(200, b"[]")

        client = ApiClient(
            "https://api.example",
            "t",
            min_incident_interval_sec=MIN_INCIDENT_ENDPOINT_INTERVAL_SEC,
            opener=opener,
        )
        path = "/sessions/s1/incidents"
        client.get_json(path)
        client.get_json(path)
        sleep.assert_called_once()
        slept = sleep.call_args[0][0]
        self.assertGreaterEqual(
            slept,
            MIN_INCIDENT_ENDPOINT_INTERVAL_SEC - 0.05,
            msg="rate gate should sleep ~5s between incident GETs",
        )


if __name__ == "__main__":
    unittest.main()
