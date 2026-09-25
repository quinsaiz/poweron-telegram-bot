import unittest
from datetime import timedelta

from poweron_live_fixtures import (
    LIVE_DATE_GRAPH,
    LIVE_EVENT_ID,
    LIVE_GROUP,
    live_collection,
    live_empty_collection,
    live_half_hour_times,
)
from pydantic import ValidationError
from src.poweron.schemas import BuildingGroupsResponse, ScheduleResponse, parse_date_graph


class BuildingGroupsResponseTests(unittest.TestCase):
    def test_exact_singleton_is_authoritative(self) -> None:
        response = BuildingGroupsResponse.model_validate({"buildingGroups": [{"chergGpv": "2.2"}]})

        self.assertEqual(response.normalized_groups(), ("2.2",))
        self.assertEqual(response.authoritative_group(), "2.2")

    def test_whitespace_and_unknown_fields_are_accepted(self) -> None:
        response = BuildingGroupsResponse.model_validate(
            {
                "buildingGroups": [{"chergGpv": " 2.2 ", "future": True}],
                "futureTopLevel": {},
            }
        )

        self.assertEqual(response.authoritative_group(), "2.2")

    def test_identical_duplicates_normalize_to_one_group(self) -> None:
        response = BuildingGroupsResponse.model_validate(
            {"buildingGroups": [{"chergGpv": "2.2"}, {"chergGpv": " 2.2 "}]}
        )

        self.assertEqual(response.normalized_groups(), ("2.2",))
        self.assertEqual(response.authoritative_group(), "2.2")

    def test_distinct_groups_are_ambiguous_in_stable_order(self) -> None:
        response = BuildingGroupsResponse.model_validate(
            {
                "buildingGroups": [
                    {"chergGpv": "4.1"},
                    {"chergGpv": "2.2"},
                    {"chergGpv": "4.1"},
                ]
            }
        )

        self.assertEqual(response.normalized_groups(), ("4.1", "2.2"))
        self.assertIsNone(response.authoritative_group())

    def test_empty_list_is_not_authoritative(self) -> None:
        response = BuildingGroupsResponse.model_validate({"buildingGroups": []})

        self.assertIsNone(response.authoritative_group())

    def test_wrong_types_and_malformed_fields_are_rejected(self) -> None:
        invalid_payloads: tuple[dict[str, object], ...] = (
            {},
            {"buildingGroups": None},
            {"buildingGroups": {}},
            {"buildingGroups": [{}]},
            {"buildingGroups": [{"chergGpv": None}]},
            {"buildingGroups": [{"chergGpv": 2.2}]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                BuildingGroupsResponse.model_validate(payload)

    def test_invalid_group_grammar_is_rejected(self) -> None:
        invalid_groups = ("2", "2.", ".2", "2.2.1", "2,2", "２.２", "-2.2", "a.b")
        for group in invalid_groups:
            with self.subTest(group=group), self.assertRaises(ValidationError):
                BuildingGroupsResponse.model_validate({"buildingGroups": [{"chergGpv": group}]})

    def test_size_bounds_are_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            BuildingGroupsResponse.model_validate(
                {"buildingGroups": [{"chergGpv": "1" * 31 + ".1"}]}
            )
        with self.assertRaises(ValidationError):
            BuildingGroupsResponse.model_validate({"buildingGroups": [{"chergGpv": "2.2"}] * 101})


class ScheduleResponseTests(unittest.TestCase):
    def test_null_members_with_zero_total_is_empty(self) -> None:
        response = ScheduleResponse.model_validate(live_empty_collection())

        self.assertEqual(response.events, [])

    def test_empty_list_with_zero_total_remains_empty(self) -> None:
        response = ScheduleResponse.model_validate({"hydra:totalItems": 0, "hydra:member": []})

        self.assertEqual(response.events, [])

    def test_null_members_with_positive_total_is_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            ScheduleResponse.model_validate({"hydra:totalItems": 1, "hydra:member": None})

    def test_null_members_without_total_is_invalid(self) -> None:
        with self.assertRaises(ValidationError):
            ScheduleResponse.model_validate({"hydra:member": None})

    def test_null_members_with_malformed_total_is_invalid(self) -> None:
        totals: tuple[object, ...] = ("0", 0.0, False, None, [], {})
        for total in totals:
            with self.subTest(total=total), self.assertRaises(ValidationError):
                ScheduleResponse.model_validate({"hydra:totalItems": total, "hydra:member": None})

    def test_incomplete_member_is_preserved_for_per_event_usability_check(self) -> None:
        response = ScheduleResponse.model_validate(
            {"hydra:totalItems": 1, "hydra:member": [{"id": 1, "dataJson": {}}]}
        )

        self.assertEqual(len(response.events), 1)
        self.assertIsNone(response.events[0].date_graph)

    def test_malformed_member_does_not_hide_valid_sibling(self) -> None:
        response = ScheduleResponse.model_validate(
            {
                "hydra:member": [
                    "not-an-event",
                    {
                        "id": 7,
                        "dateGraph": "2026-09-22T00:00:00+03:00",
                        "dataJson": {"3.2": {"times": {"00:00": "0"}}},
                    },
                ]
            }
        )

        self.assertIsNone(response.events[0].id)
        self.assertEqual(response.events[1].id, 7)

    def test_live_z_member_passes_pydantic_validation(self) -> None:
        response = ScheduleResponse.model_validate(live_collection())

        self.assertEqual(len(response.events), 1)
        event = response.events[0]
        self.assertEqual(event.id, LIVE_EVENT_ID)
        self.assertEqual(event.date_graph, LIVE_DATE_GRAPH)
        self.assertEqual(event.data_json[LIVE_GROUP]["times"], live_half_hour_times())


class DateGraphTests(unittest.TestCase):
    def test_canonical_timestamp_boundaries_are_valid(self) -> None:
        valid_values = {
            "2026-04-10T00:00:00Z": timedelta(0),
            "2026-09-22T00:00:00+03:00": timedelta(hours=3),
            "2024-02-29T23:59:59+02:00": timedelta(hours=2),
            "2026-01-01T12:30:45-05:30": -timedelta(hours=5, minutes=30),
            "2026-01-01T12:30:45+23:59": timedelta(hours=23, minutes=59),
        }

        for value, expected_offset in valid_values.items():
            with self.subTest(value=value):
                parsed = parse_date_graph(value)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.utcoffset(), expected_offset)

    def test_noncanonical_or_impossible_timestamps_are_invalid(self) -> None:
        invalid_values = (
            "2026-W39-2",
            "2026-09-22",
            "20260922T000000+0300",
            "2026-09-22 00:00:00+03:00",
            "2026-09-22t00:00:00+03:00",
            "2026/09/22T00:00:00+03:00",
            "２０２６-０９-２２T００:００:００+０３:００",
            "2026-13-22T00:00:00+03:00",
            "2026-02-30T00:00:00+03:00",
            "2026-09-22T24:00:00+03:00",
            "2026-09-22T23:60:00+03:00",
            "2026-09-22T23:59:60+03:00",
            "2026-09-22T00:00:00",
            "2026-09-22T00:00:00z",
            "2026-09-22T00:00:00.000Z",
            "2026-09-22T00:00:00+3:00",
            "2026-09-22T00:00:00+24:00",
            "2026-09-22T00:00:00+03:60",
            "2026-09-22T00:00:00Ztrailing",
            "2026-09-22T00:00:00+03:00trailing",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(parse_date_graph(value))
