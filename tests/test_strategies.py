"""Deterministic candidate checks; synthetic fixtures do not prove returns."""

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from strategy_lab.models import Bar, Contract, IST, Quote
from strategy_lab.strategies import build_plan, explain_signal


DAY = date(2026, 9, 22)  # Tuesday; avoids accidental dependence on today's date.
EXPIRY = date(2026, 9, 29)


def candles(market="NIFTY", direction=0):
    start = datetime(2026, 9, 22, 9, 15, tzinfo=IST) if market == "NIFTY" else datetime(2026, 9, 22, 16, tzinfo=IST)
    result = []
    for index in range(12):
        if market == "NIFTY":
            close = 25_000 + (5 if index % 2 else -5)
            result.append(Bar(start + timedelta(minutes=5 * index), 25_000,
                              25_030 if index == 0 else 25_010,
                              24_970 if index == 0 else 24_990, close, 100))
        else:
            close = 250 + (index * direction if direction else (0.5 if index % 2 else -0.5))
            result.append(Bar(start + timedelta(minutes=5 * index), close - direction * 0.6,
                              close + 0.3, close - 0.8 if direction >= 0 else close - 0.3,
                              close, 100))
            if direction < 0:
                result[-1] = replace(result[-1], high=close + 0.8)
    return result


def quote(market, strike, option, bid, ask, now, lot=65, expiry=EXPIRY, size=100_000):
    prefix = "NIFTY" if market == "NIFTY" else "NATGASMINI"
    contract = Contract(f"{prefix}{expiry:%d%b%y}{option}{strike}", f"{market}:{option}:{strike}",
                        "NFO" if market == "NIFTY" else "MCX", expiry, strike, option, lot, 0.05)
    return Quote(contract, now, bid, ask, (bid + ask) / 2, size, size)


def nifty_fixture():
    bars = candles()
    now = bars[-1].timestamp + timedelta(minutes=5)
    quotes = [quote("NIFTY", 24_950, "PE", 30, 31, now),
              quote("NIFTY", 24_900, "PE", 14, 15, now),
              quote("NIFTY", 25_050, "CE", 30, 31, now),
              quote("NIFTY", 25_100, "CE", 14, 15, now)]
    return bars, quotes, now


def mcx_fixture(direction=1):
    bars = candles("MCX", direction)
    now = bars[-1].timestamp + timedelta(minutes=5)
    short, hedge, option = (260, 255, "PE") if direction == 1 else (240, 245, "CE")
    quotes = [quote("MCX", short, option, 4.5, 4.6, now, lot=250),
              quote("MCX", hedge, option, 1.8, 1.9, now, lot=250)]
    return bars, quotes, now


class SignalTests(unittest.TestCase):
    def test_range_day_is_eligible_and_trending_day_is_excluded(self):
        bars, _, now = nifty_fixture()
        signal = explain_signal("NIFTY", bars, now)
        self.assertTrue(signal["eligible"], signal)
        self.assertEqual(signal["direction"], 0)
        trend = [replace(bar, open=25_000 + index * 20, high=25_015 + index * 20,
                         low=24_985 + index * 20, close=25_010 + index * 20) for index, bar in enumerate(bars)]
        self.assertFalse(explain_signal("NIFTY", trend, now)["eligible"])

    def test_mcx_requires_confirmed_direction_and_rejects_chop(self):
        for direction in (1, -1):
            bars, _, now = mcx_fixture(direction)
            signal = explain_signal("MCX", bars, now)
            self.assertTrue(signal["eligible"], signal)
            self.assertEqual(signal["direction"], direction)
        bars = candles("MCX")
        self.assertFalse(explain_signal("MCX", bars, bars[-1].timestamp + timedelta(minutes=5))["eligible"])

    def test_in_progress_and_future_bar_prices_cannot_change_signal(self):
        bars, _, now = nifty_fixture()
        expected = explain_signal("NIFTY", bars, now)
        unclosed = Bar(now, float("nan"), float("nan"), float("nan"), float("nan"))
        future = Bar(now + timedelta(minutes=5), 1, 1_000_000, 1, 1_000_000)
        self.assertEqual(expected, explain_signal("NIFTY", bars + [unclosed, future], now))

    def test_prior_day_data_cannot_warm_up_todays_signal(self):
        bars, _, now = nifty_fixture()
        old = [replace(bar, timestamp=bar.timestamp - timedelta(days=1)) for bar in bars]
        signal = explain_signal("NIFTY", old + bars[-5:], now)
        self.assertFalse(signal["eligible"])
        self.assertIn("12 completed", signal["reason"])

    def test_chronology_duplicates_gaps_bad_alignment_and_stale_data(self):
        bars, _, now = nifty_fixture()
        cases = [bars[::-1], bars + [bars[-1]], bars[:5] + bars[6:],
                 [replace(bars[0], timestamp=bars[0].timestamp + timedelta(seconds=1))] + bars[1:]]
        for malformed in cases:
            with self.subTest(case=malformed[0].timestamp):
                self.assertFalse(explain_signal("NIFTY", malformed, now)["eligible"])
        self.assertFalse(explain_signal("NIFTY", bars, now + timedelta(minutes=5))["eligible"])

    def test_malformed_prices_and_naive_time_fail_closed(self):
        bars, _, now = nifty_fixture()
        for bad in (float("nan"), float("inf"), 0, -1):
            malformed = bars[:-1] + [replace(bars[-1], close=bad)]
            self.assertFalse(explain_signal("NIFTY", malformed, now)["eligible"])
        for bar in (replace(bars[-1], high=24_000), replace(bars[-1], volume=-1),
                    replace(bars[-1], timestamp=bars[-1].timestamp.replace(tzinfo=None))):
            self.assertFalse(explain_signal("NIFTY", bars[:-1] + [bar], now)["eligible"])
        self.assertFalse(explain_signal("NIFTY", bars, now.replace(tzinfo=None))["eligible"])

    def test_entry_window_weekend_and_unknown_market(self):
        bars, _, now = nifty_fixture()
        self.assertFalse(explain_signal("NIFTY", bars, now.replace(hour=14, minute=0))["eligible"])
        self.assertFalse(explain_signal("NIFTY", bars, now.replace(day=26))["eligible"])
        self.assertFalse(explain_signal("BANKNIFTY", bars, now)["eligible"])


class ConstructionTests(unittest.TestCase):
    def test_condor_is_otm_same_expiry_hedge_first_and_payoff_bounded(self):
        bars, quotes, now = nifty_fixture()
        plan = build_plan("NIFTY", bars, quotes, now, 1, 200_000)
        self.assertIsNotNone(plan)
        self.assertEqual([leg.side for leg in plan.legs], ["BUY", "BUY", "SELL", "SELL"])
        self.assertEqual({leg.quote.contract.expiry for leg in plan.legs}, {EXPIRY})
        self.assertEqual({leg.quantity for leg in plan.legs}, {65})
        self.assertEqual(plan.max_loss, 1500)
        credit = sum((leg.quote.bid if leg.side == "SELL" else -leg.quote.ask) * leg.quantity for leg in plan.legs)
        for spot in (0, 24_900, 24_950, 25_000, 25_050, 25_100, 1_000_000):
            payoff = credit
            for leg in plan.legs:
                contract = leg.quote.contract
                intrinsic = max(0, spot - contract.strike) if contract.option_type == "CE" else max(0, contract.strike - spot)
                payoff += intrinsic * leg.quantity * (1 if leg.side == "BUY" else -1)
            self.assertGreaterEqual(payoff - 200, -plan.max_loss)
        self.assertLessEqual(plan.stop_loss, 1000)
        self.assertGreater(plan.take_profit, 0)

    def test_mcx_directional_spreads_have_hedges_in_both_directions(self):
        for direction, option in ((1, "PE"), (-1, "CE")):
            bars, quotes, now = mcx_fixture(direction)
            plan = build_plan("MCX", bars, quotes, now, 1, 200_000)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.direction, direction)
            self.assertEqual([leg.side for leg in plan.legs], ["BUY", "SELL"])
            self.assertEqual({leg.quote.contract.option_type for leg in plan.legs}, {option})
            self.assertLessEqual(plan.max_loss, 2000)
            self.assertEqual(plan.max_loss, 800)

    def test_full_size_mcx_contract_is_rejected_when_risk_exceeds_budget(self):
        bars, quotes, now = mcx_fixture()
        large = [replace(q, contract=replace(q.contract, symbol=q.contract.symbol.replace("NATGASMINI", "NATURALGAS"), lot_size=1250)) for q in quotes]
        self.assertIsNone(build_plan("MCX", bars, large, now, 1, 200_000))

    def test_capital_and_multiplier_bounds_and_linear_scaling(self):
        bars, quotes, now = nifty_fixture()
        for multiplier, capital in ((0, 200_000), (-1, 200_000), (True, 200_000), (1.5, 400_000),
                                    (1, 199_999), (2, 399_999), (1, float("nan")), (1, float("inf")),
                                    (10 ** 1000, 200_000), (1, 10 ** 1000)):
            self.assertIsNone(build_plan("NIFTY", bars, quotes, now, multiplier, capital))
        one = build_plan("NIFTY", bars, quotes, now, 1, 200_000)
        two = build_plan("NIFTY", bars, quotes, now, 2, 400_000)
        self.assertEqual(two.max_loss, one.max_loss * 2)
        self.assertEqual(two.take_profit, one.take_profit * 2)
        self.assertEqual([leg.quantity for leg in two.legs], [130] * 4)
        # Spare capital does not silently expand a one-multiplier risk budget.
        dearer = [replace(q, bid=q.bid - 10, ask=q.ask - 10) if q.contract.strike in (24_950, 25_050) else q for q in quotes]
        self.assertIsNone(build_plan("NIFTY", bars, dearer, now, 1, 2_000_000))

    def test_no_mixed_expiry_lot_or_underlying_hedges(self):
        bars, quotes, now = nifty_fixture()
        for contract in (replace(quotes[1].contract, expiry=EXPIRY + timedelta(days=7)),
                         replace(quotes[1].contract, lot_size=75),
                         replace(quotes[1].contract, symbol="BANKNIFTY29SEP26P24900")):
            self.assertIsNone(build_plan("NIFTY", bars, [quotes[0], replace(quotes[1], contract=contract)] + quotes[2:], now, 1, 200_000))

    def test_expiry_day_and_expiry_eve_are_excluded(self):
        bars, quotes, now = nifty_fixture()
        for expiry in (DAY, DAY + timedelta(days=1)):
            near = [replace(q, contract=replace(q.contract, expiry=expiry)) for q in quotes]
            self.assertIsNone(build_plan("NIFTY", bars, near, now, 1, 200_000))

    def test_quotes_must_be_fresh_liquid_and_well_formed(self):
        bars, quotes, now = nifty_fixture()
        original = quotes[0]
        bad = [replace(original, timestamp=now - timedelta(seconds=11)),
               replace(original, timestamp=now + timedelta(seconds=1)),
               replace(original, timestamp=now.replace(tzinfo=None)),
               replace(original, bid=float("nan")), replace(original, ask=float("inf")),
               replace(original, bid=0), replace(original, last=-1),
               replace(original, bid=33, ask=31), replace(original, bid=20, ask=31),
               replace(original, bid=30.01), replace(original, bid_size=64),
               replace(original, bid_size=-1), replace(original, bid_size=True),
               replace(original, contract=replace(original.contract, token="")),
               replace(original, contract=replace(original.contract, tick_size=0))]
        for replacement in bad:
            with self.subTest(quote=replacement):
                self.assertIsNone(build_plan("NIFTY", bars, [replacement] + quotes[1:], now, 1, 200_000))
        no_hedge_depth = quotes[:1] + [replace(quotes[1], ask_size=64)] + quotes[2:]
        self.assertIsNone(build_plan("NIFTY", bars, no_hedge_depth, now, 1, 200_000))

    def test_duplicate_instrument_snapshots_are_not_cherry_picked(self):
        bars, quotes, now = nifty_fixture()
        self.assertIsNone(build_plan("NIFTY", bars, quotes + [quotes[0]], now, 1, 200_000))

    def test_live_price_not_ltp_determines_executable_credit(self):
        bars, quotes, now = nifty_fixture()
        base = build_plan("NIFTY", bars, quotes, now, 1, 200_000)
        stale_ltp = [replace(q, last=1_000_000) for q in quotes]
        other = build_plan("NIFTY", bars, stale_ltp, now, 1, 200_000)
        self.assertEqual(base.max_loss, other.max_loss)
        self.assertEqual(base.take_profit, other.take_profit)


if __name__ == "__main__":
    unittest.main()
