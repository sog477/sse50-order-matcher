"""Observable historical book + separate zero-impact shadow order and capacity ledger."""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path

from orderbook import (OrderBook, apply_market_event, cents, continuous, dataset_date,
                       load_stock_events, parse_time, price_str, _market_rows)


def stamp_ms(stamp: int) -> int:
    h, tail = divmod(int(stamp), 10000000)
    m, tail = divmod(tail, 100000)
    s, ms = divmod(tail, 1000)
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def ms_stamp(ms: int) -> int:
    h, tail = divmod(int(ms), 3600000)
    m, tail = divmod(tail, 60000)
    s, ms = divmod(tail, 1000)
    return h * 10000000 + m * 100000 + s * 1000 + ms


def clock(stamp: int) -> str:
    text = f'{stamp:09d}'
    return f'{text[:2]}:{text[2:4]}:{text[4:6]}.{text[6:]}'


def phase(stamp: int) -> str:
    if 91500000 <= stamp < 92500000:
        return '开盘集合竞价'
    if continuous(stamp):
        return '连续竞价'
    if 145700000 <= stamp < 150000000:
        return '收盘集合竞价'
    if 113000000 <= stamp < 130000000:
        return '午间休市'
    if stamp >= 150000000:
        return '竞价结束'
    return '开盘等待'


@dataclass(frozen=True)
class Fees:
    commission: str = '0.0002'  # configurable broker assumption, inclusive of exchange levies
    minimum_commission_cents: int = 500
    transfer: str = '0.00001'
    sell_stamp: str = '0.0005'

    def total(self, notional_cents: int, side: str) -> dict:
        def rounded(rate):
            return int((Decimal(notional_cents) * Decimal(rate)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        commission = max(self.minimum_commission_cents, rounded(self.commission)) if notional_cents else 0
        transfer = rounded(self.transfer)
        stamp = rounded(self.sell_stamp) if side == 'S' else 0
        return {'commission': commission, 'transfer': transfer, 'stamp': stamp,
                'total': commission + transfer + stamp}


@dataclass
class VirtualOrder:
    id: str
    side: str
    kind: str
    price: int | None
    quantity: int
    remaining: int
    created: int
    protection: int | None = None
    status: str = 'OPEN'
    notional_cents: int = 0
    fees_cents: int = 0
    note: str = ''


@lru_cache(maxsize=2)
def cached_events(data: str, symbol: int):
    return load_stock_events(Path(data), symbol)


class HistoricalSession:
    """One outstanding virtual order per stock/session, all historical messages immutable."""
    def __init__(self, symbol: int, events: list[tuple], preclose: int = 0, fees: Fees | None = None,
                 allow_residual_fills: bool = True):
        self.symbol, self.events, self.preclose = symbol, events, preclose
        self.book = OrderBook(symbol, retain_trades=False)
        self.cursor = 0
        self.time = 91500000
        self.stats = defaultdict(int)
        self.orders: list[VirtualOrder] = []
        self.fills: list[dict] = []
        self.used: dict[str, int] = defaultdict(int)
        self.ahead: set[str] = set()
        self.tape = deque(maxlen=30)
        self.fees = fees or Fees()
        self.closing_auction_done = False
        self.allow_residual_fills = allow_residual_fills

    def fork_market(self, allow_residual_fills: bool = True):
        """Independent experiment at this historical cursor, with fresh virtual capacity.

        The event stream is read-only and shared. Historical state is copied; manual
        orders, fills and used capacity belong to a separate experiment, not a portfolio.
        """
        branch = HistoricalSession(self.symbol, self.events, self.preclose, self.fees, allow_residual_fills)
        branch.book = deepcopy(self.book)
        branch.cursor, branch.time = self.cursor, self.time
        branch.stats = defaultdict(int, self.stats)
        branch.tape = deque(self.tape, maxlen=30)
        branch.closing_auction_done = self.closing_auction_done
        return branch

    @classmethod
    def from_data(cls, data: Path, symbol: int):
        events, excluded = cached_events(str(data.resolve()), symbol)
        rows = _market_rows(data / f'snp_{dataset_date(data)}.parquet', symbol, ['Symbol', 'PreClose'])
        preclose = next((cents(round(float(r['PreClose']), 2)) for r in rows if r['PreClose'] > 0), 0)
        session = cls(symbol, events, preclose)
        session.excluded_after_hours = excluded
        session.advance(93000000)
        return session

    @property
    def active(self):
        return next((o for o in self.orders if o.remaining and o.status in ('OPEN', 'OPEN_AUCTION')), None)

    def _fill(self, order: VirtualOrder, price: int, quantity: int, model: str, seq: int | None = None):
        if not 0 < quantity <= order.remaining:
            raise ValueError('模拟成交超出剩余量')
        if order.protection is not None and ((order.side == 'B' and price > order.protection) or (order.side == 'S' and price < order.protection)):
            raise ValueError('模拟成交越过保护限价')
        order.remaining -= quantity
        order.notional_cents += price * quantity
        cumulative = self.fees.total(order.notional_cents, order.side)
        increment = cumulative['total'] - order.fees_cents
        order.fees_cents = cumulative['total']
        if not order.remaining:
            order.status = 'FILLED'
        self.fills.append({'order_id': order.id, 'time': clock(self.time), 'price': price_str(price),
                           'volume': quantity, 'side': order.side, 'model': model, 'historical_seq': seq,
                           'notional': price_str(price * quantity), 'fee': price_str(increment)})

    def _queue(self, order):
        self.ahead = {oid for oid, r in self.book.orders.items() if r.side == order.side and r.price == order.price and r.remaining > self.used.get(oid, 0)}

    def _sweep(self, order, cap: int, levels: int | None = None):
        other = 'S' if order.side == 'B' else 'B'
        prices = list(self.book.prices[other]) if other == 'S' else list(reversed(self.book.prices[other]))
        if levels is not None:
            prices = prices[:levels]
        last = None
        for price in prices:
            if (order.side == 'B' and price > cap) or (order.side == 'S' and price < cap):
                break
            # Read the actual FIFO order queue, not order ID magnitude.
            for oid in list(self.book.queues[other][price]):
                r = self.book.orders.get(oid)
                if r is None:
                    continue
                capacity = max(0, r.remaining - self.used[oid])
                quantity = min(capacity, order.remaining)
                if quantity:
                    self._fill(order, price, quantity, 'book-sweep')
                    self.used[oid] += quantity
                    last = price
                if not order.remaining:
                    return last
        return last

    def submit(self, side: str, kind: str, quantity: int, price=None, protection=None):
        if self.active:
            raise ValueError('请先撤销或等待当前未完成委托')
        in_auction = 145700000 <= self.time < 150000000
        if not continuous(self.time) and not (in_auction and kind == 'LIMIT'):
            raise ValueError('当前阶段不能提交这种委托')
        if side not in ('B', 'S') or type(quantity) is not int or quantity <= 0:
            raise ValueError('方向或数量无效')
        if kind not in ('LIMIT', 'FIVE_CANCEL', 'FIVE_LIMIT', 'SAME_BEST', 'OPPOSITE_BEST'):
            raise ValueError('未知委托类型')
        star = str(self.symbol).startswith('688')
        maximum = (100000 if kind == 'LIMIT' else 50000) if star else 1000000
        if quantity > maximum or (side == 'B' and ((star and quantity < 200) or (not star and quantity % 100))):
            raise ValueError('申报数量不符合该板块限制')
        p = cents(price) if kind == 'LIMIT' else None
        protect = cents(protection) if kind != 'LIMIT' else None
        order = VirtualOrder(f'V{len(self.orders)+1}', side, kind, p, quantity, quantity, self.time, protect)
        self.orders.append(order)
        if in_auction:
            order.status = 'OPEN_AUCTION'
            order.note = '等待15:00集合竞价场景定价'
            self._queue(order)
            return self.state()
        if kind == 'LIMIT':
            self._sweep(order, p)
        elif kind in ('FIVE_CANCEL', 'FIVE_LIMIT'):
            own = self.book._best(side)
            last = self._sweep(order, protect, 5)
            if order.remaining:
                if kind == 'FIVE_CANCEL':
                    order.status, order.note = 'CANCELLED', '五档即时成交后剩余撤销'
                else:
                    order.price = last if last is not None else own
                    if order.price is None or (side == 'B' and order.price > protect) or (side == 'S' and order.price < protect):
                        order.status, order.note = 'CANCELLED', '无可用转限价或价格超出保护范围，模型撤销'
                    else:
                        order.note = '市价剩余转限价'
        else:
            reference = side if kind == 'SAME_BEST' else ('S' if side == 'B' else 'B')
            order.price = self.book._best(reference)
            if order.price is None or (side == 'B' and order.price > protect) or (side == 'S' and order.price < protect):
                order.status, order.note = 'CANCELLED', '无报价或报价超出保护范围，模型撤销'
            else:
                self._sweep(order, order.price)
        if order.status == 'OPEN':
            self._queue(order)
        return self.state()

    def cancel(self):
        order = self.active
        if order is None:
            raise ValueError('没有未完成委托')
        if 145700000 <= self.time < 150000000 or 92000000 <= self.time < 92500000:
            raise ValueError('此集合竞价阶段不接受撤单')
        order.status, order.note = 'CANCELLED', '用户撤单，剩余量不再参与撮合'
        self.ahead.clear()
        return self.state()

    def _closing_auction(self):
        order = self.active
        self.closing_auction_done = True
        if order is None:
            return
        branch = OrderBook(self.symbol)
        entries = [(r.entered_at, i, oid, r.side, r.price, max(0, r.remaining - self.used[oid]))
                   for i, (oid, r) in enumerate(self.book.orders.items())]
        entries.append((order.created, len(entries), order.id, order.side, order.price, order.remaining))
        for entered, _, oid, side, price, qty in sorted(entries):
            if qty:
                branch.add_resting(oid, side, price, qty, entered)
        result = branch.auction(150000000)
        for fill in result['trades']:
            own = fill['buy_order_id'] if order.side == 'B' else fill['sell_order_id']
            if own == order.id:
                self._fill(order, cents(fill['price']), fill['volume'], 'closing-auction-branch')
        if order.remaining:
            order.status, order.note = 'EXPIRED', '集合竞价后未成交部分当日失效'

    def _before_event(self, event):
        time, channel, seq, kind, row = event
        order = self.active
        if time == 150000000 and kind == 'exe' and row['BSFlag'] == 0 and not self.closing_auction_done:
            self._closing_auction()
            order = self.active
        if kind == 'exe':
            passive = 'S' if row['BSFlag'] == 66 else 'B' if row['BSFlag'] == 83 else None
            if passive is None:
                return
            oid = f"{channel}:{row['AskOrder'] if passive == 'S' else row['BidOrder']}"
            volume = int(row['Volume'])
            overlap = min(volume, self.used.get(oid, 0))
            self.used[oid] = max(0, self.used.get(oid, 0) - overlap)
            budget = volume - overlap
            if order and continuous(time) and order.side == passive and order.status == 'OPEN':
                p = cents(round(float(row['Price']), 2))
                through = p < order.price if order.side == 'B' else p > order.price
                ahead_live = any(k in self.book.orders and self.book.orders[k].remaining > self.used.get(k, 0) for k in self.ahead)
                at_queue = p == order.price and oid not in self.ahead and not ahead_live
                if budget and (through or at_queue):
                    self._fill(order, order.price, min(order.remaining, budget), 'historical-queue', seq)
        elif self.allow_residual_fills and row['OrderKind'] == 65 and order and continuous(time) and order.status == 'OPEN':
            p = cents(round(float(row['Price']), 2))
            crosses = p <= order.price if order.side == 'B' else p >= order.price
            if chr(row['FunctionCode']) != order.side and crosses:
                # This new opposite residual is observable NOW, after its historical initial fills.
                qty = min(order.remaining, int(row['Volume']))
                self._fill(order, order.price, qty, 'new-opposite-residual', seq)
                self.used[f"{channel}:{row['OrderOriNo']}"] += qty

    def advance(self, target: int):
        target = parse_time(target)
        if target < self.time or target > 150000000:
            raise ValueError('回放必须向前且不超过15:00；重置可返回09:30')
        while self.cursor < len(self.events) and self.events[self.cursor][0] <= target:
            event = self.events[self.cursor]
            self.time = event[0]
            order = self.active
            if order and self.time >= 145700000 and order.status == 'OPEN':
                order.status, order.note = 'OPEN_AUCTION', '未成交限价单进入收盘集合竞价'
            self._before_event(event)
            apply_market_event(self.book, event, self.stats)
            _, channel, _, kind, row = event
            if kind == 'exe':
                self.tape.append({'time': clock(event[0]), 'price': price_str(cents(round(float(row['Price']), 2))),
                                  'volume': int(row['Volume']), 'side': 'B' if row['BSFlag'] == 66 else 'S' if row['BSFlag'] == 83 else '集合', 'seq': event[2]})
                ids = [f"{channel}:{row['BidOrder']}", f"{channel}:{row['AskOrder']}"]
            else:
                ids = [f"{channel}:{row['OrderOriNo']}"]
            for oid in ids:
                if oid not in self.book.orders:
                    self.used.pop(oid, None)
                elif self.used.get(oid, 0) > self.book.orders[oid].remaining:
                    self.used[oid] = self.book.orders[oid].remaining
            self.cursor += 1
        self.time = target
        if self.active and target >= 145700000 and self.active.status == 'OPEN':
            self.active.status = 'OPEN_AUCTION'
        if target >= 150000000 and not self.closing_auction_done:
            self._closing_auction()
        return self.state()

    def state(self):
        active = self.active
        ahead = sum(max(0, self.book.orders[k].remaining - self.used.get(k, 0)) for k in self.ahead if k in self.book.orders) if active else 0
        orders = []
        for order in self.orders:
            row = asdict(order)
            row.update(price=price_str(order.price), protection=price_str(order.protection), created=clock(order.created),
                       notional=price_str(order.notional_cents), fees=price_str(order.fees_cents))
            orders.append(row)
        return {'symbol': self.symbol, 'time': self.time, 'clock': clock(self.time), 'phase': phase(self.time),
                'preclose': price_str(self.preclose), 'events': self.cursor, 'book': self.book.snapshot(self.time),
                'orders': orders, 'fills': self.fills[-100:], 'market_trades': list(reversed(self.tape)),
                'ahead_shares': ahead, 'used_capacity_shares': sum(self.used.values()),
                'total_fees': price_str(sum(o.fees_cents for o in self.orders)),
                'fee_model': asdict(self.fees), 'fees_breakdown': {key: price_str(sum(self.fees.total(o.notional_cents, o.side)[key] for o in self.orders)) for key in ('commission', 'transfer', 'stamp')},
                'historical_immutable': True}
