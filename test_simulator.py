import unittest
from orderbook import OrderBook
from simulator import HistoricalSession, Fees


def order(seq, oid, side, price, qty, action=65, time=93001000):
    return (time, 1001, seq, 'ord', {'OrderOriNo': oid, 'OrderKind': action, 'FunctionCode': ord(side), 'Price': price, 'Volume': qty})


def trade(seq, buy, sell, qty, price=10, time=93001000, flag=83):
    return (time, 1001, seq, 'exe', {'BidOrder': buy, 'AskOrder': sell, 'BSFlag': flag, 'Price': price, 'Volume': qty})


class ShadowTests(unittest.TestCase):
    def session(self, events=None):
        s = HistoricalSession(600000, events or [])
        s.time = 93000000
        return s

    def test_immediate_fill_does_not_modify_history_or_reuse_capacity(self):
        s = self.session()
        s.book.add_resting('1001:1', 'S', 1000, 100, s.time)
        s.submit('B', 'LIMIT', 100, '10.00')
        self.assertEqual(s.book.orders['1001:1'].remaining, 100)
        s.submit('B', 'LIMIT', 100, '10.00')
        self.assertEqual(s.active.remaining, 100)
        self.assertEqual(sum(f['volume'] for f in s.fills), 100)

    def test_fifo_queue_requires_trade_after_ahead_is_gone(self):
        events = [trade(1, 1, 90, 100), order(2, 2, 'B', 10, 500), trade(3, 2, 91, 100)]
        s = self.session(events)
        s.book.add_resting('1001:1', 'B', 1000, 100, s.time)
        s.submit('B', 'LIMIT', 100, '10')
        s.advance(93001000)
        self.assertEqual(s.orders[0].status, 'FILLED')
        self.assertEqual(s.fills[0]['historical_seq'], 3)
        self.assertEqual(s.book.orders['1001:2'].remaining, 400)

    def test_cancel_ahead_never_generates_fill(self):
        s = self.session([order(1, 1, 'B', 10, 100, 68)])
        s.book.add_resting('1001:1', 'B', 1000, 100, s.time)
        s.submit('B', 'LIMIT', 100, '10')
        s.advance(93001000)
        self.assertEqual(s.active.remaining, 100)
        self.assertFalse(s.fills)
        s.cancel()
        self.assertEqual(s.orders[0].status, 'CANCELLED')

    def test_future_opposite_residual_and_capacity(self):
        s = self.session([order(1, 1, 'S', 10, 100)])
        s.submit('B', 'LIMIT', 100, '10')
        self.assertFalse(s.fills)
        s.advance(93001000)
        self.assertEqual(s.fills[0]['model'], 'new-opposite-residual')
        self.assertEqual(s.used['1001:1'], 100)
        self.assertEqual(s.book.orders['1001:1'].remaining, 100)

    def test_five_levels_not_five_orders(self):
        s = self.session()
        for i in range(6):
            s.book.add_resting(str(i), 'S', 1000, 100, s.time)
        s.book.add_resting('higher', 'S', 1001, 100, s.time)
        s.submit('B', 'FIVE_CANCEL', 700, protection='10.02')
        self.assertEqual(s.orders[0].status, 'FILLED')
        self.assertEqual(sum(f['volume'] for f in s.fills), 700)

    def test_market_residual_cancel_or_convert(self):
        s = self.session()
        s.book.add_resting('ask', 'S', 1000, 100, s.time)
        s.submit('B', 'FIVE_CANCEL', 200, protection='10')
        self.assertEqual(s.orders[0].status, 'CANCELLED')
        self.assertEqual(s.orders[0].remaining, 100)
        t = self.session()
        t.book.add_resting('ask', 'S', 1000, 100, t.time)
        t.submit('B', 'FIVE_LIMIT', 200, protection='10')
        self.assertEqual(t.active.price, 1000)
        self.assertEqual(t.active.remaining, 100)

    def test_same_and_opposite_best_and_protection(self):
        s = self.session()
        s.book.add_resting('bid', 'B', 999, 100, s.time)
        s.book.add_resting('ask', 'S', 1000, 100, s.time)
        s.submit('B', 'SAME_BEST', 100, protection='10.01')
        self.assertEqual(s.active.price, 999)
        self.assertFalse(s.fills)
        s.cancel()
        s.submit('B', 'OPPOSITE_BEST', 200, protection='10.01')
        self.assertEqual(s.active.remaining, 100)
        self.assertEqual(s.active.price, 1000)
        t = self.session()
        t.book.add_resting('ask', 'S', 1001, 100, t.time)
        t.submit('B', 'FIVE_CANCEL', 100, protection='10')
        self.assertFalse(t.fills)

    def test_min_commission_is_per_order(self):
        s = self.session([order(1, 1, 'S', 10, 50), order(2, 2, 'S', 10, 50)])
        s.submit('B', 'LIMIT', 100, '10')
        s.advance(93001000)
        self.assertEqual(s.orders[0].fees_cents, 501)
        self.assertEqual([f['fee'] for f in s.fills], ['5.01', '0.00'])
        self.assertEqual(Fees().total(100000, 'S')['stamp'], 50)

    def test_same_time_split_advance_equals_one_jump(self):
        events = [order(1, 1, 'B', 10, 100), trade(2, 1, 90, 100)]
        a, b = self.session(events), self.session(events)
        a.advance(93000500)
        a.advance(93001000)
        b.advance(93001000)
        self.assertEqual(a.book.snapshot(a.time), b.book.snapshot(b.time))

    def test_closing_branch_carries_limit_and_preserves_history(self):
        s = self.session([trade(1, 1, 2, 100, time=150000000, flag=0)])
        s.time = 145600000
        s.book.add_resting('1001:1', 'B', 1000, 100, 93000000)
        s.book.add_resting('1001:2', 'S', 1000, 200, 145700000)
        # Queue a buy during the collection phase without immediately crossing.
        s.time = 145700000
        s.submit('B', 'LIMIT', 100, '10')
        self.assertFalse(s.fills)
        with self.assertRaises(ValueError):
            s.cancel()
        s.advance(150000000)
        self.assertEqual(s.orders[0].status, 'FILLED')
        self.assertEqual(s.fills[0]['model'], 'closing-auction-branch')
        self.assertEqual(s.book.orders['1001:2'].remaining, 100)


class AuctionTests(unittest.TestCase):
    def test_midpoint_tie_and_fifo_partial(self):
        b = OrderBook(600000)
        b.add_resting('b1', 'B', 1003, 100, 1)
        b.add_resting('b2', 'B', 1003, 200, 2)
        b.add_resting('s', 'S', 1001, 300, 3)
        result = b.auction(92500000)
        self.assertEqual(result['price'], '10.02')
        self.assertEqual([t['buy_order_id'] for t in result['trades']], ['b1', 'b2'])
        self.assertEqual(result['volume'], 300)

    def test_no_cross_and_half_up_tick(self):
        b = OrderBook(600000)
        b.add_resting('b', 'B', 1000, 100, 1)
        b.add_resting('s', 'S', 1001, 100, 2)
        self.assertEqual(b.auction(92500000)['volume'], 0)
        b = OrderBook(600000)
        b.add_resting('b', 'B', 1002, 100, 1)
        b.add_resting('s', 'S', 1001, 100, 2)
        self.assertEqual(b.auction(92500000)['price'], '10.02')


if __name__ == '__main__':
    unittest.main()
