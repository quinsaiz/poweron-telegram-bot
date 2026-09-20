import unittest

from pydantic import ValidationError
from src.poweron.schemas import ScheduleResponse


class ScheduleResponseTests(unittest.TestCase):
    def test_null_members_with_zero_total_is_empty(self):
        response = ScheduleResponse.model_validate({"hydra:totalItems": 0, "hydra:member": None})

        self.assertEqual(response.events, [])

    def test_empty_list_with_zero_total_remains_empty(self):
        response = ScheduleResponse.model_validate({"hydra:totalItems": 0, "hydra:member": []})

        self.assertEqual(response.events, [])

    def test_null_members_with_positive_total_is_invalid(self):
        with self.assertRaises(ValidationError):
            ScheduleResponse.model_validate({"hydra:totalItems": 1, "hydra:member": None})

    def test_null_members_without_total_is_invalid(self):
        with self.assertRaises(ValidationError):
            ScheduleResponse.model_validate({"hydra:member": None})

    def test_null_members_with_malformed_total_is_invalid(self):
        for total in ("0", 0.0, False, None, [], {}):
            with self.subTest(total=total), self.assertRaises(ValidationError):
                ScheduleResponse.model_validate({"hydra:totalItems": total, "hydra:member": None})

    def test_malformed_populated_member_is_invalid(self):
        for total in (0, 1):
            with self.subTest(total=total), self.assertRaises(ValidationError):
                ScheduleResponse.model_validate(
                    {
                        "hydra:totalItems": total,
                        "hydra:member": [{"id": 1, "dataJson": {}}],
                    }
                )
