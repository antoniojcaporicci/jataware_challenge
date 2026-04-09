from __future__ import annotations

import http.client
import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

from nightwatch_worker.catalog import (
    CatalogCache,
    IncidentPlanningState,
    eligible_next_actions,
    incident_type_mappable,
    parse_catalog_body,
)
from nightwatch_worker.http_client import ApiClient


def _sample_catalog() -> dict:
    return {
        "api_version": "v2",
        "actions": [
            {
                "action_id": "sync",
                "dependencies": [],
                "parallel": True,
            },
            {
                "action_id": "diagnose",
                "dependencies": ["sync"],
                "parallel": False,
            },
            {
                "action_id": "remediate",
                "dependencies": ["diagnose"],
                "serial": True,
            },
        ],
        "playbooks": {
            "disk_full": ("sync", "diagnose", "remediate"),
        },
    }


def _nightwatch_style_catalog() -> dict:
    """Shape from real API (session catalog): actions dict + catalog.type.resolution_actions."""
    return {
        "api_version": "v2",
        "catalog_revision": 1,
        "actions": {
            "compare_cache_snapshot": {
                "execution": "parallel",
                "duration_sec": 5,
            },
            "info": {
                "execution": "serial",
                "duration_sec": 1,
            },
        },
        "catalog": {
            "cache_drift": {
                "incident_type": "cache_drift",
                "resolution_actions": [
                    {"action_id": "info"},
                    {
                        "action_id": "compare_cache_snapshot",
                        "depends_on": ["info"],
                    },
                ],
            }
        },
    }


class TestParseCatalog(unittest.TestCase):
    def test_parses_nightwatch_catalog_dict_actions_and_resolution_playbook(self) -> None:
        snap = parse_catalog_body(_nightwatch_style_catalog())
        self.assertIn("info", snap.actions)
        self.assertTrue(snap.actions["compare_cache_snapshot"].parallel)
        self.assertFalse(snap.actions["info"].parallel)
        self.assertEqual(
            frozenset(["info"]),
            snap.actions["compare_cache_snapshot"].dependencies,
        )
        book = snap.playbook("cache_drift")
        assert book is not None
        self.assertEqual(book, ("info", "compare_cache_snapshot"))
        self.assertEqual(
            snap.playbook("cache_drift_2"),
            ("info", "compare_cache_snapshot"),
        )
        self.assertTrue(incident_type_mappable(snap, "cache_drift_2"))

    def test_parses_actions_and_playbooks(self) -> None:
        snap = parse_catalog_body(_sample_catalog())
        self.assertIn("sync", snap.actions)
        self.assertTrue(snap.actions["sync"].parallel)
        self.assertFalse(snap.actions["remediate"].parallel)
        self.assertEqual(frozenset(["sync"]), snap.actions["diagnose"].dependencies)
        book = snap.playbook("disk_full")
        assert book is not None
        self.assertEqual(book, ("sync", "diagnose", "remediate"))

    def test_non_dict_body_empty_snapshot(self) -> None:
        snap = parse_catalog_body(None)
        self.assertEqual(len(snap.actions), 0)
        self.assertEqual(len(snap.playbooks), 0)

    def test_incident_type_mappable(self) -> None:
        snap = parse_catalog_body(_sample_catalog())
        self.assertTrue(incident_type_mappable(snap, "disk_full"))
        self.assertFalse(incident_type_mappable(snap, "unknown"))


class TestEligibleNextActions(unittest.TestCase):
    def setUp(self) -> None:
        self.snap = parse_catalog_body(_sample_catalog())

    def test_nightwatch_catalog_cache_drift_2_eligible_info(self) -> None:
        snap = parse_catalog_body(_nightwatch_style_catalog())
        st = IncidentPlanningState(incident_type="cache_drift_2")
        self.assertEqual(eligible_next_actions(snap, st), ["info"])

    def test_first_parallel_when_no_deps_done(self) -> None:
        st = IncidentPlanningState(incident_type="disk_full")
        self.assertEqual(eligible_next_actions(self.snap, st), ["sync"])

    def test_serial_after_parallel_completed(self) -> None:
        st = IncidentPlanningState(
            incident_type="disk_full",
            completed_action_ids=frozenset({"sync"}),
        )
        self.assertEqual(eligible_next_actions(self.snap, st), ["diagnose"])

    def test_blocked_while_serial_in_flight(self) -> None:
        st = IncidentPlanningState(
            incident_type="disk_full",
            completed_action_ids=frozenset({"sync"}),
            in_flight_action_ids=frozenset({"diagnose"}),
        )
        self.assertEqual(eligible_next_actions(self.snap, st), [])

    def test_finished_or_not_accepting(self) -> None:
        st = IncidentPlanningState(incident_type="disk_full", finished=True)
        self.assertEqual(eligible_next_actions(self.snap, st), [])
        st2 = IncidentPlanningState(incident_type="disk_full", accepts_actions=False)
        self.assertEqual(eligible_next_actions(self.snap, st2), [])

    def test_unknown_incident_type(self) -> None:
        st = IncidentPlanningState(incident_type="other")
        self.assertEqual(eligible_next_actions(self.snap, st), [])

    def test_parallel_cap_two(self) -> None:
        payload = {
            "actions": [
                {"action_id": "p1", "parallel": True},
                {"action_id": "p2", "parallel": True},
                {"action_id": "p3", "parallel": True},
            ],
            "playbooks": {"wide": ("p1", "p2", "p3")},
        }
        snap = parse_catalog_body(payload)
        st = IncidentPlanningState(incident_type="wide")
        self.assertEqual(set(eligible_next_actions(snap, st)), {"p1", "p2"})

    def test_parallel_in_flight_respects_cap(self) -> None:
        payload = {
            "actions": [
                {"action_id": "p1", "parallel": True},
                {"action_id": "p2", "parallel": True},
            ],
            "playbooks": {"wide": ("p1", "p2")},
        }
        snap = parse_catalog_body(payload)
        st = IncidentPlanningState(
            incident_type="wide",
            in_flight_action_ids=frozenset({"p1"}),
        )
        self.assertEqual(eligible_next_actions(snap, st), ["p2"])


class TestCatalogCache(unittest.TestCase):
    def _opener_json(self, status: int, obj: object) -> MagicMock:
        raw = json.dumps(obj).encode("utf-8")

        class _CM:
            def __enter__(self) -> _CM:
                return self

            def __exit__(self, *a: object) -> None:
                pass

            def read(self) -> bytes:
                return raw

        opener = MagicMock()
        cm = _CM()
        setattr(cm, "status", status)
        setattr(cm, "headers", http.client.HTTPMessage())
        opener.open.return_value = cm
        return opener

    def test_success_sets_last_fetched_and_snapshot(self) -> None:
        catalog = _sample_catalog()
        opener = self._opener_json(200, catalog)
        client = ApiClient("https://api.example", "tok", opener=opener)
        cache = CatalogCache(retry_5xx_attempts=0, retry_network_attempts=0)
        log = MagicMock()
        with patch("nightwatch_worker.catalog.time.time", return_value=12345.0):
            r = cache.maybe_refresh(client, "s1", log=log, now_monotonic=100.0)
        self.assertTrue(r.success)
        self.assertEqual(r.last_fetched_at, 12345.0)
        self.assertIsNotNone(r.snapshot)
        assert r.snapshot is not None
        self.assertIn("disk_full", r.snapshot.playbooks)
        self.assertFalse(r.used_stale)

    def test_failure_keeps_stale_snapshot(self) -> None:
        opener = MagicMock()

        class _CM:
            def __init__(self, status: int, body: bytes) -> None:
                self.status = status
                self._b = body
                self.headers = http.client.HTTPMessage()

            def __enter__(self) -> _CM:
                return self

            def __exit__(self, *a: object) -> None:
                pass

            def read(self) -> bytes:
                return self._b

        good = _CM(200, json.dumps(_sample_catalog()).encode())
        err = urllib.error.HTTPError(
            "http://x",
            503,
            "x",
            hdrs={},
            fp=BytesIO(b"{}"),
        )

        def open_side_effect(*a: object, **k: object) -> object:
            open_side_effect.n += 1  # type: ignore[attr-defined]
            if open_side_effect.n == 1:
                return good
            raise err

        open_side_effect.n = 0  # type: ignore[attr-defined]

        opener.open.side_effect = open_side_effect
        client = ApiClient("https://api.example", "tok", opener=opener)
        cache = CatalogCache(retry_5xx_attempts=0, retry_network_attempts=0)
        cache.maybe_refresh(client, "s1", now_monotonic=0.0)
        r2 = cache.maybe_refresh(client, "s1", now_monotonic=1.0)
        self.assertFalse(r2.success)
        self.assertTrue(r2.used_stale)
        self.assertIsNotNone(r2.snapshot)

    def test_backoff_skips_request_until_due(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("no network")
        client = ApiClient("https://api.example", "tok", opener=opener)
        cache = CatalogCache(retry_5xx_attempts=0, retry_network_attempts=0)
        with patch("nightwatch_worker.catalog.time.monotonic", return_value=0.0):
            with patch("nightwatch_worker.catalog.time.sleep"):
                r1 = cache.maybe_refresh(client, "s1", now_monotonic=0.0)
        self.assertFalse(r1.success)
        opener.open.assert_called()
        calls_after = opener.open.call_count
        r2 = cache.maybe_refresh(client, "s1", now_monotonic=0.1)
        self.assertFalse(r2.success)
        self.assertEqual(opener.open.call_count, calls_after)


if __name__ == "__main__":
    unittest.main()
