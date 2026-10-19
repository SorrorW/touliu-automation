from __future__ import annotations

import datetime
import unittest

from 巨量千川.plan_name import with_delivery_date_prefix
from 巨量千川.plan_power import _needs_8am_delivery_date_stamp


class DeliveryDatePrefixTests(unittest.TestCase):
    DAY = datetime.date(2026, 8, 26)

    def test_replaces_standard_mmdd_prefix(self) -> None:
        self.assertEqual(
            with_delivery_date_prefix("0825_达人A_赵", self.DAY),
            "0826_达人A_赵",
        )

    def test_normalizes_historic_separated_prefix(self) -> None:
        self.assertEqual(
            with_delivery_date_prefix("6.23-达人A-赵", self.DAY),
            "0826_达人A-赵",
        )

    def test_adds_prefix_when_old_plan_has_none(self) -> None:
        self.assertEqual(
            with_delivery_date_prefix("达人A_赵", self.DAY),
            "0826_达人A_赵",
        )

    def test_does_not_confuse_leading_id_with_date(self) -> None:
        self.assertEqual(
            with_delivery_date_prefix("65617822705", self.DAY),
            "0826_65617822705",
        )

    def test_only_queued_8am_new_plans_get_the_delivery_date_stamp(self) -> None:
        self.assertTrue(_needs_8am_delivery_date_stamp({
            "category": "new_plan", "resume_time": "08:00",
        }))
        self.assertFalse(_needs_8am_delivery_date_stamp({
            "category": "old_pool", "resume_time": "08:00",
        }))
        self.assertFalse(_needs_8am_delivery_date_stamp({
            "category": "new_plan", "resume_time": "12:00",
        }))


if __name__ == "__main__":
    unittest.main()
