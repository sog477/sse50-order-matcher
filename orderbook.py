"""SSE A-share continuous-auction simulator and historical L3 replay.

Prices are integer cents. Historical executions are authoritative observations;
the simulator never manufactures them while replaying market data.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, insort
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
import heapq
import json
import re
from pathlib import Path


def cents(value: object) -> int:
    price = Decimal(str(value))
    scaled = price * 100
    if not price.is_finite() or price <= 0 or scaled != scaled.to_integral_value():
        raise ValueError(f"价格须为正数且精确到分: {value}")
    return int(scaled)


def price_str(value: int | None) -> str | None:
    return None if value is None else f"{Decimal(value) / 100:.2f}"


def parse_time(value: str | int) -> int:
    text = str(value)
    if ":" in text:
        match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", text)
        if not match:
            raise ValueError("时间须为 HH:MM:SS.mmm 或 HHMMSSmmm")
        h, m, s, fraction = match.groups()
        raw = f"{int(h):02d}{m}{s}{(fraction or '').ljust(3, '0')}"
    else:
        raw = text
    if not raw.isdigit() or len(raw) > 9:
        raise ValueError("时间须为 HH:MM:SS.mmm 或 HHMMSSmmm")
    raw = raw.zfill(9)
    h, m, s, ms = int(raw[:2]), int(raw[2:4]), int(raw[4:6]), int(raw[6:])
    if h > 23 or m > 59 or s > 59 or ms > 999:
        raise ValueError(f"无效时间: {value}")
    return int(raw)


def continuous(time: int) -> bool:
    return 93000000 <= time < 113000000 or 130000000 <= time < 145700000


@dataclass
class Order:
    order_id: str
    side: str
    price: int
    remaining: int
    entered_at: int


@dataclass
class Trade:
    time: int
    price: str
    volume: int
    buy_order_id: str
    sell_order_id: str
    source: str = "simulated"


class OrderBook:
    def __init__(self, symbol: int, *, retain_trades: bool = True):
        self.symbol = symbol
        self.orders: dict[str, Order] = {}
        self.queues: dict[str, dict[int, deque[str]]] = {
            "B": defaultdict(deque), "S": defaultdict(deque)
        }
        self.heaps: dict[str, list[int]] = {"B": [], "S": []}
        self.levels: dict[str, dict[int, int]] = {"B": {}, "S": {}}
        self.prices: dict[str, list[int]] = {"B": [], "S": []}
        self.retain_trades = retain_trades
        self.pre_add_active_legs = 0
        self.last_price: int | None = None
        self.trades: list[Trade] = []
        self.total_volume = 0
        self.total_turnover_cents = 0

    def _best(self, side: str) -> int | None:
        heap = self.heaps[side]
        queues = self.queues[side]
        while heap:
            price = -heap[0] if side == "B" else heap[0]
            q = queues[price]
            while q and (q[0] not in self.orders or self.orders[q[0]].remaining <= 0):
                q.popleft()
            if q:
                return price
            heapq.heappop(heap)
            del queues[price]
        return None

    def add_resting(self, order_id: str, side: str, price: int, volume: int, time: int) -> None:
        if side not in ("B", "S") or price <= 0 or volume <= 0:
            raise ValueError("无效订单")
        if order_id in self.orders:
            raise ValueError(f"重复订单号: {order_id}")
        self.orders[order_id] = Order(order_id, side, price, volume, time)
        if not self.queues[side][price]:
            heapq.heappush(self.heaps[side], -price if side == "B" else price)
        self.queues[side][price].append(order_id)
        if price not in self.levels[side]:
            insort(self.prices[side], price)
            self.levels[side][price] = 0
        self.levels[side][price] += volume

    def cancel(self, order_id: str, volume: int | None = None) -> int:
        order = self.orders.get(order_id)
        if order is None:
            return 0
        removed = order.remaining if volume is None else volume
        if removed <= 0 or removed > order.remaining:
            raise ValueError(f"数量扣减异常: {order_id}, 剩余={order.remaining}, 扣减={removed}")
        order.remaining -= removed
        levels = self.levels[order.side]
        levels[order.price] -= removed
        if levels[order.price] == 0:
            del levels[order.price]
            self.prices[order.side].pop(bisect_left(self.prices[order.side], order.price))
        if order.remaining == 0:
            del self.orders[order_id]
        return removed

    def _record(self, time: int, price: int, volume: int, buy: str, sell: str, source: str) -> Trade:
        trade = Trade(time, price_str(price), volume, buy, sell, source)
        if self.retain_trades:
            self.trades.append(trade)
        self.last_price = price
        self.total_volume += volume
        self.total_turnover_cents += price * volume
        return trade

    def submit(self, order_id: str, side: str, limit_price: int, volume: int,
               time: int, *, validate_session: bool = True) -> list[Trade]:
        if validate_session and not continuous(time):
            raise ValueError("仅支持连续竞价时段 09:30–11:30、13:00–14:57")
        if side not in ("B", "S") or limit_price <= 0 or volume <= 0:
            raise ValueError("无效限价订单")
        if 688000 <= self.symbol <= 689999:
            if volume > 100000 or (side == "B" and volume < 200):
                raise ValueError("科创板限价买单须为 200–100000 股；卖单不超过 100000 股")
        elif side == "B" and (volume % 100 or volume > 1000000):
            raise ValueError("主板买单须为 100 股整数倍且不超过 100 万股")
        elif volume > 1000000:
            raise ValueError("单笔申报不得超过 100 万股")
        if order_id in self.orders:
            raise ValueError(f"重复订单号: {order_id}")
        other = "S" if side == "B" else "B"
        remaining = volume
        trades: list[Trade] = []
        while remaining:
            best = self._best(other)
            if best is None or (side == "B" and best > limit_price) or (side == "S" and best < limit_price):
                break
            resting_id = self.queues[other][best][0]
            resting = self.orders[resting_id]
            filled = min(remaining, resting.remaining)
            buy, sell = (order_id, resting_id) if side == "B" else (resting_id, order_id)
            trades.append(self._record(time, best, filled, buy, sell, "simulated"))
            remaining -= filled
            self.cancel(resting_id, filled)
        if remaining:
            self.add_resting(order_id, side, limit_price, remaining, time)
        return trades

    def apply_execution(self, buy_id: str, sell_id: str, price: int, volume: int, time: int,
                        aggressor: str | None = None) -> tuple[Trade, int]:
        """Missing pre-ADD aggressors are expected; passive/auction legs must exist."""
        if aggressor not in (None, "B", "S") or volume <= 0 or price <= 0:
            raise ValueError("无效成交事件")
        # Validate all legs before modifying state, so an exception is atomic.
        for side, order_id in (("B", buy_id), ("S", sell_id)):
            order = self.orders.get(order_id)
            if order is None:
                if side != aggressor:
                    raise ValueError(f"被动/集合竞价成交挂单缺失: {order_id}")
            elif order.side != side or volume > order.remaining:
                raise ValueError(f"成交方向或数量异常: {order_id}, 剩余={order.remaining}, 成交={volume}")
        for side, order_id in (("B", buy_id), ("S", sell_id)):
            if order_id in self.orders:
                self.cancel(order_id, volume)
            else:
                self.pre_add_active_legs += 1
        return self._record(time, price, volume, buy_id, sell_id, "historical"), 0

    def snapshot(self, time: int, depth: int = 10) -> dict:
        def levels(side: str) -> list[dict]:
            ranked = self.prices[side][-depth:][::-1] if side == "B" else self.prices[side][:depth]
            return [{"price": price_str(price), "volume": self.levels[side][price]} for price in ranked]

        return {"symbol": self.symbol, "time": time, "bids": levels("B"),
                "asks": levels("S"), "last_price": price_str(self.last_price),
                "acc_volume": self.total_volume,
                "acc_turnover": f"{Decimal(self.total_turnover_cents) / 100:.2f}"}

    def auction(self, time: int) -> dict:
        """Single-price call auction; candidates and tie-breaks follow SSE 3.5.2."""
        prices = sorted(set(self.prices['B']) | set(self.prices['S']))
        if not prices:
            return {'price': None, 'volume': 0, 'trades': []}
        bids, asks = self.levels['B'], self.levels['S']
        demand = sum(bids.values())
        supply = 0
        candidates = []
        for price in prices:
            supply += asks.get(price, 0)
            matched = min(demand, supply)
            if matched and demand - bids.get(price, 0) <= matched and supply - asks.get(price, 0) <= matched:
                if demand == matched or supply == matched:
                    candidates.append((matched, abs(demand - supply), price))
            demand -= bids.get(price, 0)
        if not candidates:
            return {'price': None, 'volume': 0, 'trades': []}
        maximum = max(v for v, _, _ in candidates)
        imbalance = min(g for v, g, _ in candidates if v == maximum)
        finalists = [p for v, g, p in candidates if v == maximum and g == imbalance]
        clearing = int((Decimal(min(finalists) + max(finalists)) / 2).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        fills = []
        while True:
            bid, ask = self._best('B'), self._best('S')
            if bid is None or ask is None or bid < clearing or ask > clearing:
                break
            buy = self.queues['B'][bid][0]
            sell = self.queues['S'][ask][0]
            quantity = min(self.orders[buy].remaining, self.orders[sell].remaining)
            fills.append(self._record(time, clearing, quantity, buy, sell, 'auction'))
            self.cancel(buy, quantity)
            self.cancel(sell, quantity)
        return {'price': price_str(clearing), 'volume': sum(t.volume for t in fills),
                'trades': [asdict(t) for t in fills]}


def _market_rows(path: Path, symbol: int, columns: list[str]) -> list[dict]:
    try:
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("历史回放需要 pyarrow：python -m pip install pyarrow") from exc
    rows: list[dict] = []
    reader = pq.ParquetFile(path)
    for batch in reader.iter_batches(batch_size=65536, columns=columns):
        selected = batch.filter(pc.equal(batch.column(batch.schema.get_field_index("Symbol")), symbol))
        if selected.num_rows:
            rows.extend(selected.to_pylist())
    return rows


def dataset_date(data_dir: Path) -> str:
    names = list(data_dir.glob("ord_????????.parquet"))
    if len(names) != 1:
        raise ValueError("数据目录须包含且仅包含一个交易日的 ord_YYYYMMDD.parquet")
    return names[0].stem.split("_")[1]


def load_stock_events(data_dir: Path, symbol: int) -> tuple[list[tuple], int]:
    date = dataset_date(data_dir)
    ord_rows = _market_rows(data_dir / f"ord_{date}.parquet", symbol,
                            ["Symbol", "Time", "BizIndex", "Channel", "OrderOriNo", "FunctionCode", "OrderKind", "Price", "Volume"])
    exe_rows = _market_rows(data_dir / f"exe_{date}.parquet", symbol,
                            ["Symbol", "Time", "BizIndex", "Channel", "AskOrder", "BidOrder", "BSFlag", "Price", "Volume"])
    events = [(int(r["Time"]), int(r["Channel"]), int(r["BizIndex"]), "ord", r) for r in ord_rows]
    events += [(int(r["Time"]), int(r["Channel"]), int(r["BizIndex"]), "exe", r) for r in exe_rows if r["Time"] <= 150000000]
    excluded = sum(r["Time"] > 150000000 for r in exe_rows)
    if not events:
        raise ValueError(f"证券 {symbol} 没有竞价事件")
    if len({e[1] for e in events}) != 1:
        raise ValueError("多频道证券需要独立业务序列映射，不能用时间戳混排")
    events.sort(key=lambda x: x[2])
    if len({e[2] for e in events}) != len(events):
        raise ValueError("频道内业务序号重复")
    if any(b[0] < a[0] for a, b in zip(events, events[1:])):
        raise ValueError("业务序号排序后时间倒退")
    return events, excluded


def apply_market_event(book: OrderBook, event: tuple, stats: dict) -> None:
    time, channel, seq, kind, row = event
    stats["events"] += 1
    if kind == "ord":
        action = chr(int(row["OrderKind"]))
        oid = f'{channel}:{int(row["OrderOriNo"])}'
        if action == "A":
            book.add_resting(oid, chr(int(row["FunctionCode"])),
                             cents(round(float(row["Price"]), 2)), int(row["Volume"]), time)
            stats["adds"] += 1
        elif action == "D":
            if oid not in book.orders:
                raise ValueError(f"撤单挂单缺失: {oid}, BizIndex={seq}")
            if book.orders[oid].side != chr(int(row["FunctionCode"])):
                raise ValueError(f"撤单方向异常: {oid}")
            book.cancel(oid, int(row["Volume"]))
            stats["cancels"] += 1
        elif action == "S":
            stats["status_events"] += 1
        else:
            raise ValueError(f"未知委托事件类型: {action}")
    else:
        flag = int(row["BSFlag"])
        if flag not in (0, 66, 83):
            raise ValueError(f"未知成交方向: {flag}")
        book.apply_execution(f'{channel}:{int(row["BidOrder"])}', f'{channel}:{int(row["AskOrder"])}',
                             cents(round(float(row["Price"]), 2)), int(row["Volume"]), time,
                             None if flag == 0 else chr(flag))
        stats["executions"] += 1


def replay(data_dir: Path, symbol: int, at: list[int]) -> tuple[list[dict], dict, OrderBook]:
    """Replay only competition events in verified channel BizIndex order."""
    if not at:
        raise ValueError("至少指定一个快照时间")
    events, excluded = load_stock_events(data_dir, symbol)
    book = OrderBook(symbol, retain_trades=False)
    stats = defaultdict(int)
    snapshots = []
    i = 0
    for target in sorted(set(at)):
        while i < len(events) and events[i][0] <= target:
            apply_market_event(book, events[i], stats)
            i += 1
        snapshots.append(book.snapshot(target))
    stats["input_events"] = len(events)
    stats["excluded_after_hours_trades"] = excluded
    stats["expected_pre_add_active_legs"] = book.pre_add_active_legs
    stats["active_orders"] = len(book.orders)
    stats["channels"] = sorted({e[1] for e in events})
    return snapshots, dict(stats), book


def reference_snapshot(data_dir: Path, symbol: int, time: int) -> dict | None:
    columns = ["Symbol", "Time", "Price", "AccVolume", "AccTurnover"]
    for side in ("Bid", "Ask"):
        columns += [f"{side}Price{i}" for i in range(1, 11)]
        columns += [f"{side}Volume{i}" for i in range(1, 11)]
    rows = _market_rows(data_dir / f"snp_{dataset_date(data_dir)}.parquet", symbol, columns)
    matches = [row for row in rows if int(row["Time"]) == time]
    if not matches:
        return None
    row = matches[-1]
    levels = {}
    for side, key in (("Bid", "bids"), ("Ask", "asks")):
        levels[key] = [{"price": price_str(int(round(float(row[f"{side}Price{i}"]) * 100))),
                        "volume": int(row[f"{side}Volume{i}"])}
                       for i in range(1, 11) if row[f"{side}Price{i}"] and row[f"{side}Volume{i}"]]
    return {"symbol": symbol, "time": time, **levels,
            "last_price": price_str(int(round(float(row["Price"]) * 100))) if row["Price"] else None,
            "acc_volume": int(row["AccVolume"]), "acc_turnover": str(row["AccTurnover"])}


def compare_snapshot(rebuilt: dict, reference: dict | None) -> dict | None:
    if reference is None:
        return None
    result = {"acc_volume_equal": rebuilt["acc_volume"] == reference["acc_volume"],
              "acc_turnover_equal": Decimal(rebuilt["acc_turnover"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP) == Decimal(reference["acc_turnover"]),
              "last_price_equal": rebuilt["last_price"] == reference["last_price"]}
    for side in ("bids", "asks"):
        pairs = list(zip(rebuilt[side], reference[side]))
        result[f"{side}_matching_levels"] = sum(a == b for a, b in pairs)
        result[f"{side}_reference_levels"] = len(reference[side])
    return result


def demo() -> dict:
    book = OrderBook(600000)
    time = parse_time("09:30:00.000")
    book.add_resting("sell-1", "S", cents("10.01"), 500, time)
    book.add_resting("sell-2", "S", cents("10.02"), 1000, time)
    trades = book.submit("buy-1", "B", cents("10.02"), 800, time)
    return {"trades": [asdict(t) for t in trades], "snapshot": book.snapshot(time)}


def main() -> None:
    parser = argparse.ArgumentParser(description="上交所连续竞价撮合与逐笔数据回放")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="运行 10.01/10.02 示例")
    trade = sub.add_parser("trade", help="模拟限价单撮合；从 JSON 订单簿加载")
    trade.add_argument("--book", type=Path, required=True, help='JSON：{"symbol":...,"orders":[{"id","side","price","volume"}]}')
    trade.add_argument("--id", required=True)
    trade.add_argument("--side", choices=["B", "S"], required=True)
    trade.add_argument("--price", required=True)
    trade.add_argument("--volume", type=int, required=True)
    trade.add_argument("--time", default="09:30:00.000")
    rep = sub.add_parser("replay", help="从本地逐笔数据重建并生成指定时点快照")
    rep.add_argument("--data", type=Path, default=Path("sse50_20260923"))
    rep.add_argument("--symbol", type=int, required=True)
    rep.add_argument("--at", action="append", required=True, help="可重复，例如 --at 09:30:00.000")
    rep.add_argument("--compare", action="store_true", help="若该时点有原始快照，则一并输出供对照")
    sim = sub.add_parser("simulate", help="在指定时点的历史重建簿上提交模拟限价单")
    sim.add_argument("--data", type=Path, default=Path("sse50_20260923"))
    sim.add_argument("--symbol", type=int, required=True)
    sim.add_argument("--at", required=True)
    sim.add_argument("--id", required=True)
    sim.add_argument("--side", choices=["B", "S"], required=True)
    sim.add_argument("--price", required=True)
    sim.add_argument("--volume", type=int, required=True)
    args = parser.parse_args()
    if args.command == "demo":
        result = demo()
    elif args.command == "trade":
        setup = json.loads(args.book.read_text(encoding="utf-8"))
        book = OrderBook(int(setup["symbol"]))
        time = parse_time(args.time)
        for row in setup["orders"]:
            book.add_resting(str(row["id"]), row["side"], cents(row["price"]), int(row["volume"]), time)
        fills = book.submit(args.id, args.side, cents(args.price), args.volume, time)
        result = {"trades": [asdict(t) for t in fills], "snapshot": book.snapshot(time)}
    elif args.command == "replay":
        snaps, stats, _ = replay(args.data, args.symbol, [parse_time(t) for t in args.at])
        result = {"snapshots": snaps, "replay_stats": stats}
        if args.compare:
            references = [reference_snapshot(args.data, args.symbol, s["time"]) for s in snaps]
            result["reference_snapshots"] = references
            result["comparison"] = [compare_snapshot(s, r) for s, r in zip(snaps, references)]
    else:
        time = parse_time(args.at)
        snaps, stats, book = replay(args.data, args.symbol, [time])
        fills = book.submit(args.id, args.side, cents(args.price), args.volume, time)
        result = {"before": snaps[0], "trades": [asdict(t) for t in fills],
                  "after": book.snapshot(time), "replay_stats": stats}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
