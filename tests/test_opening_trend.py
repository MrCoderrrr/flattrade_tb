import unittest
from datetime import datetime, timedelta

from strategy_lab.models import Bar, IST
from strategy_lab.opening_trend import opening_drive


class OpeningTrendTests(unittest.TestCase):
    def bars(self, step, count=20):
        start = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
        rows = []
        price = 23000.0
        for minute in range(count):
            close = price + step
            rows.append(Bar(start + timedelta(minutes=minute), price,
                            max(price, close) + 1, min(price, close) - 1,
                            close, 1000, 1))
            price = close
        return rows

    def test_sustained_opening_down_move(self):
        rows = self.bars(-4)
        for minute in (5, 11, 15, 20):
            with self.subTest(minute=minute):
                self.assertEqual(opening_drive(rows, rows[0].timestamp + timedelta(minutes=minute)), -1)

    def test_sustained_opening_up_move(self):
        rows = self.bars(4)
        self.assertEqual(opening_drive(rows, rows[0].timestamp + timedelta(minutes=20)), 1)

    def test_no_drive_after_opening_window_or_with_gap(self):
        rows = self.bars(-4, 22)
        self.assertEqual(opening_drive(rows, rows[0].timestamp + timedelta(minutes=21)), 0)
        self.assertEqual(opening_drive(rows[:6] + rows[7:], rows[0].timestamp + timedelta(minutes=18)), 0)


if __name__ == '__main__':
    unittest.main()
