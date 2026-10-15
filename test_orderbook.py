"""Regression checks for publication semantics and matching invariants."""
import unittest
from collections import defaultdict

from orderbook import OrderBook, apply_market_event, cents, demo, parse_time


class LifecycleTests(unittest.TestCase):
    def test_cross_level_example(self):
        result = demo()
        self.assertEqual([(t['price'], t['volume']) for t in result['trades']], [('10.01', 500), ('10.02', 300)])
        self.assertEqual(result['snapshot']['asks'], [{'price': '10.02', 'volume': 700}])

    def test_pre_add_active_execution_then_residual(self):
        b = OrderBook(600000)
        b.add_resting('1001:1', 'S', 1001, 500, 93000000)
        b.apply_execution('1001:2', '1001:1', 1001, 500, 93000001, 'B')
        # This ADD contains only the residual. Do not subtract the previous 500 again.
        b.add_resting('1001:2', 'B', 1001, 300, 93000002)
        self.assertEqual(b.orders['1001:2'].remaining, 300)
        self.assertEqual(b.pre_add_active_legs, 1)
        self.assertEqual(b.total_volume, 500)

    def test_missing_passive_fails_before_changing_state(self):
        b = OrderBook(600000)
        b.add_resting('buy', 'B', 1000, 100, 92500000)
        with self.assertRaises(ValueError):
            b.apply_execution('buy', 'unknown-sell', 1000, 100, 92500000)
        self.assertEqual(b.orders['buy'].remaining, 100)
        self.assertEqual(b.total_volume, 0)

    def test_overdraw_is_not_clipped(self):
        b = OrderBook(600000)
        b.add_resting('sell', 'S', 1000, 100, 93000000)
        with self.assertRaises(ValueError):
            b.cancel('sell', 101)
        with self.assertRaises(ValueError):
            b.apply_execution('active-buy', 'sell', 1000, 101, 93000000, 'B')
        self.assertEqual(b.orders['sell'].remaining, 100)

    def test_fifo_after_partial_cancel(self):
        b = OrderBook(600000)
        b.add_resting('first', 'S', 1000, 300, 93000000)
        b.add_resting('second', 'S', 1000, 500, 93000001)
        b.cancel('first', 100)
        trades = b.submit('buy', 'B', 1000, 400, 93000002)
        self.assertEqual([(t.sell_order_id, t.volume) for t in trades], [('first', 200), ('second', 200)])
        self.assertEqual(b.levels['S'][1000], 300)

    def test_missing_cancel_is_rejected(self):
        b = OrderBook(600000)
        row = {'OrderKind': 68, 'OrderOriNo': 99, 'FunctionCode': 66, 'Volume': 100}
        with self.assertRaises(ValueError):
            apply_market_event(b, (93000000, 1001, 1, 'ord', row), defaultdict(int))

    def test_clock_and_session_boundaries(self):
        self.assertEqual(parse_time('09:30:00'), 93000000)
        self.assertEqual(parse_time('09:30:00.1'), 93000100)
        b = OrderBook(600000)
        with self.assertRaises(ValueError):
            b.submit('buy', 'B', 1000, 100, 145700000)
        with self.assertRaises(ValueError):
            cents('10.001')


if __name__ == '__main__':
    unittest.main()
