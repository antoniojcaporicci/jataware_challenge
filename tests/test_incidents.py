from __future__ import annotations

import http.client
import json
import unittest
import urllib.error
from unittest.mock import MagicMock

from nightwatch_worker.catalog import CatalogCache, IncidentPlanningState
from nightwatch_worker.http_client import ApiClient
from nightwatch_worker.incidents import (
    list_row_to_state,
    merge_detail_over_list,
    needs_targeted_detail,
    open_rows_ttl_order,
    parse_incidents_list_payload,
    parse_incident_detail_payload,
    reconcile_tick,
    to_planning_state,
)


class TestParseIncidentsList(unittest.TestCase):
    def test_accepts_actions_camel_case_key(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "i1",
                    "incident_type": "cache_drift_1",
                    "status": "open",
                    "acceptActions": True,
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                }
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].accepts_known)
        self.assertIs(rows[0].accepts_actions, True)

    def test_accepts_actions_numeric_one(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "i1",
                    "incident_type": "cache_drift_1",
                    "status": "open",
                    "accepts_actions": 1,
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                }
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].accepts_known)
        self.assertIs(rows[0].accepts_actions, True)

    def test_dict_with_incidents_key(self) -> None:
        body = {
            "incidents": [
                {
                    "incident_id": "i1",
                    "incident_type": "disk_full",
                    "status": "open",
                    "accepts_actions": True,
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "ttl_seconds": 120,
                }
            ]
        }
        rows = parse_incidents_list_payload(body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].incident_id, "i1")
        self.assertTrue(rows[0].is_open)
        self.assertTrue(rows[0].actions_known)
        self.assertTrue(rows[0].accepts_known)

    def test_resolved_closed(self) -> None:
        rows = parse_incidents_list_payload(
            [{"id": "a", "type": "t", "status": "resolved", "accepts_actions": False}]
        )
        self.assertFalse(rows[0].is_open)

    def test_ttl_order_unknown_last(self) -> None:
        rows = parse_incidents_list_payload(
            {
                "incidents": [
                    {
                        "incident_id": "later",
                        "type": "t",
                        "status": "open",
                        "ttl_seconds": 300,
                        "completed_actions": [],
                        "in_flight_actions": [],
                        "failed_actions": [],
                        "accepts_actions": True,
                    },
                    {
                        "incident_id": "sooner",
                        "type": "t",
                        "status": "open",
                        "ttl_seconds": 10,
                        "completed_actions": [],
                        "in_flight_actions": [],
                        "failed_actions": [],
                        "accepts_actions": True,
                    },
                    {
                        "incident_id": "unk",
                        "type": "t",
                        "status": "open",
                        "completed_actions": [],
                        "in_flight_actions": [],
                        "failed_actions": [],
                        "accepts_actions": True,
                    },
                ]
            }
        )
        ordered = open_rows_ttl_order(rows)
        self.assertEqual([r.incident_id for r in ordered], ["sooner", "later", "unk"])


class TestNeedsTargetedDetail(unittest.TestCase):
    def test_missing_type(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "x",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "accepts_actions": True,
                }
            ]
        )
        self.assertTrue(needs_targeted_detail(rows[0]))

    def test_sufficient_list_row(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "x",
                    "type": "disk_full",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "accepts_actions": True,
                }
            ]
        )
        self.assertFalse(needs_targeted_detail(rows[0]))


class TestMergeAndPlanning(unittest.TestCase):
    def test_open_list_row_missing_accepts_is_unknown_not_false(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "i1",
                    "type": "t",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                }
            ]
        )
        st = list_row_to_state(rows[0], detail_truth=True)
        self.assertIsNone(st.accepts_actions)

    def test_merge_infers_accepts_true_when_detail_omits_field(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "i1",
                    "type": "t",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "accepts_actions": True,
                }
            ]
        )
        detail = parse_incident_detail_payload(
            "i1",
            {
                "incident_id": "i1",
                "incident_type": "t",
                "status": "open",
                "completed_actions": [],
                "in_flight_actions": [],
                "failed_actions": [],
            },
        )
        assert detail is not None
        merged = merge_detail_over_list(rows[0], detail)
        self.assertTrue(merged.detail_truth)
        self.assertIs(merged.accepts_actions, True)

    def test_merge_prefers_detail_actions(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "i1",
                    "type": "t",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "accepts_actions": True,
                }
            ]
        )
        row = rows[0]
        detail = parse_incident_detail_payload(
            "i1",
            {
                "incident_id": "i1",
                "incident_type": "t",
                "status": "open",
                "completed_actions": ["a1"],
                "in_flight_actions": [],
                "failed_actions": [],
                "accepts_actions": 1,
            },
        )
        assert detail is not None
        merged = merge_detail_over_list(row, detail)
        self.assertTrue(merged.detail_truth)
        self.assertEqual(merged.completed_action_ids, frozenset({"a1"}))

    def test_to_planning_state_requires_detail_truth(self) -> None:
        rows = parse_incidents_list_payload(
            [
                {
                    "incident_id": "x",
                    "type": "t",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "accepts_actions": True,
                }
            ]
        )
        st = list_row_to_state(rows[0], detail_truth=True)
        ps = to_planning_state(st)
        self.assertIsNotNone(ps)
        assert ps is not None
        self.assertIsInstance(ps, IncidentPlanningState)


class TestReconcileTick(unittest.TestCase):
    def _fake_cm(self, status: int, body: object) -> object:
        raw = json.dumps(body).encode()

        class CM:
            def __init__(self) -> None:
                self.status = status
                self.headers = http.client.HTTPMessage()

            def __enter__(self) -> CM:
                return self

            def __exit__(self, *a: object) -> None:
                pass

            def read(self) -> bytes:
                return raw

        return CM()

    def test_reconcile_list_and_one_detail(self) -> None:
        list_body = {
            "incidents": [
                {
                    "incident_id": "i1",
                    "status": "open",
                    "ttl_seconds": 60,
                }
            ]
        }
        detail_body = {
            "incident_id": "i1",
            "incident_type": "disk",
            "status": "open",
            "completed_actions": [],
            "in_flight_actions": [],
            "failed_actions": [],
            "accepts_actions": True,
        }
        opener = MagicMock()
        opener.open.side_effect = [
            self._fake_cm(200, list_body),
            self._fake_cm(200, detail_body),
        ]
        client = ApiClient("https://api.example", "tok", opener=opener)
        log = MagicMock()
        r = reconcile_tick(client, "s1", log, catalog_cache=None)
        self.assertEqual(r.list_http_status, 200)
        self.assertEqual(r.targeted_detail_id, "i1")
        self.assertEqual(r.detail_http_status, 200)
        self.assertEqual(len(r.open_incidents), 1)
        self.assertTrue(r.open_incidents[0].detail_truth)

        opener = MagicMock()
        opener.open.side_effect = [
            self._fake_cm(200, list_body),
        ]
        client = ApiClient("https://api.example", "tok", opener=opener)
        r2 = reconcile_tick(
            client, "s1", None, catalog_cache=None, fetch_targeted_detail=False
        )
        self.assertIsNone(r2.targeted_detail_id)
        self.assertEqual(opener.open.call_count, 1)

    def test_reconcile_executes_eligible_action(self) -> None:
        catalog_body = {
            "actions": [
                {"action_id": "a1", "parallel": True, "dependencies": []},
            ],
            "playbooks": {"t": ("a1",)},
        }
        list_body = {
            "incidents": [
                {
                    "incident_id": "i1",
                    "incident_type": "t",
                    "status": "open",
                    "completed_actions": [],
                    "in_flight_actions": [],
                    "failed_actions": [],
                    "accepts_actions": True,
                }
            ]
        }
        opener = MagicMock()
        opener.open.side_effect = [
            self._fake_cm(200, catalog_body),
            self._fake_cm(200, list_body),
            self._fake_cm(200, {}),
        ]
        client = ApiClient(
            "https://api.example",
            "tok",
            opener=opener,
            min_incident_interval_sec=0.01,
        )
        cache = CatalogCache(retry_5xx_attempts=0, retry_network_attempts=0)
        pending: dict[str, set[str]] = {}
        log = MagicMock()
        r = reconcile_tick(
            client,
            "s1",
            log,
            catalog_cache=cache,
            execute_actions=True,
            local_pending=pending,
        )
        self.assertEqual(len(r.action_attempts), 1)
        self.assertTrue(r.action_attempts[0].ok)
        self.assertEqual(r.action_attempts[0].action_id, "a1")
        self.assertIn("a1", pending["i1"])

    def test_list_network_error(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("down")
        client = ApiClient("https://api.example", "tok", opener=opener)
        r = reconcile_tick(client, "s1", None, catalog_cache=None)
        self.assertEqual(r.list_http_status, 0)
        self.assertIsNotNone(r.list_error)


if __name__ == "__main__":
    unittest.main()
