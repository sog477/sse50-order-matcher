"""Full event audit; timestamp searches are offline diagnostics, never strategy input."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import time

import pyarrow.compute as pc
import pyarrow.parquet as pq

from orderbook import (OrderBook, _market_rows, apply_market_event, cents,
                       continuous, dataset_date, load_stock_events)


def stamp_ms(stamp: int) -> int:
    h, rest = divmod(int(stamp), 10000000)
    m, rest = divmod(rest, 100000)
    s, ms = divmod(rest, 1000)
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def signature(book: OrderBook) -> tuple:
    return (book.total_volume,
            tuple((p, book.levels['B'][p]) for p in book.prices['B'][-10:][::-1]),
            tuple((p, book.levels['S'][p]) for p in book.prices['S'][:10]))


def snapshot_signature(row: dict) -> tuple:
    sides = []
    for prefix in ('Bid', 'Ask'):
        sides.append(tuple((cents(round(float(row[f'{prefix}Price{i}']), 2)), int(row[f'{prefix}Volume{i}']))
                           for i in range(1, 11) if row[f'{prefix}Price{i}'] > 0))
    return (int(row['AccVolume']), *sides)


def audit_stock(data: Path, symbol: int, stride: int = 100) -> tuple[dict, list[dict]]:
    events, excluded = load_stock_events(data, symbol)
    columns = ['Symbol', 'Time', 'AccVolume', 'AccTurnover']
    columns += [f'{side}{field}{i}' for side in ('Bid', 'Ask') for field in ('Price', 'Volume') for i in range(1, 11)]
    snapshots = sorted(_market_rows(data / f'snp_{dataset_date(data)}.parquet', symbol, columns), key=lambda r: r['Time'])
    selected = [r for r in snapshots if continuous(r['Time']) and r['Time'] not in (93000000, 113000000, 130000000)][::stride]
    book = OrderBook(symbol, retain_trades=False)
    stats = defaultdict(int)
    cursor = 0
    records = []

    for snap in selected:
        target = stamp_ms(snap['Time'])
        expected = snapshot_signature(snap)
        earlier_shift = None

        def equal() -> bool:
            return book.total_volume == expected[0] and signature(book) == expected

        while cursor < len(events) and stamp_ms(events[cursor][0]) <= target:
            event = events[cursor]
            apply_market_event(book, event, stats)
            cursor += 1
            shift = stamp_ms(event[0]) - target
            if shift >= -2000 and equal():
                earlier_shift = shift
        strict = equal()
        within_second = strict
        later_shift = 0 if strict else None
        strict_volume = book.total_volume == expected[0]
        while not within_second and cursor < len(events) and stamp_ms(events[cursor][0]) < target + 1000:
            event = events[cursor]
            apply_market_event(book, event, stats)
            cursor += 1
            if equal():
                within_second = True
                later_shift = stamp_ms(event[0]) - target
        shifts = [s for s in (earlier_shift, later_shift) if s is not None]
        records.append({'symbol': symbol, 'time': int(snap['Time']), 'strict_match': strict,
                        'strict_volume_match': strict_volume, 'same_second_match': within_second,
                        'diagnostic_match': bool(shifts),
                        'diagnostic_shift_ms': min(shifts, key=abs) if shifts else None})

    while cursor < len(events):
        apply_market_event(book, events[cursor], stats)
        cursor += 1
    # A failure in any association/quantity invariant raises before a successful row is written.
    reconstructed_total = sum(order.remaining for order in book.orders.values())
    aggregated_total = sum(sum(levels.values()) for levels in book.levels.values())
    if reconstructed_total != aggregated_total:
        raise ValueError('订单级数量与价位聚合不守恒')
    closing = [s for s in snapshots if 150000000 <= s['Time'] < 150500000]
    final = closing[-1] if closing else None
    row = {'symbol': symbol, **dict(stats), 'anomalies': 0,
           'expected_pre_add_active_legs': book.pre_add_active_legs,
           'excluded_after_hours_trades': excluded,
           'close_volume': book.total_volume, 'reference_close_volume': final['AccVolume'] if final else None,
           'close_volume_equal': bool(final and book.total_volume == final['AccVolume']),
           'close_top10_equal': bool(final and signature(book) == snapshot_signature(final)),
           'snapshots_sampled': len(records),
           'strict_matches': sum(r['strict_match'] for r in records),
           'same_second_matches': sum(r['same_second_match'] for r in records),
           'diagnostic_matches': sum(r['diagnostic_match'] for r in records)}
    return row, records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('sse50_20260923'))
    parser.add_argument('--out', type=Path, default=Path('reports'))
    parser.add_argument('--stride', type=int, default=100)
    parser.add_argument('--symbol', type=int, action='append')
    args = parser.parse_args()
    if args.stride < 1:
        parser.error('stride must be positive')
    start = time.perf_counter()
    date = dataset_date(args.data)
    symbols = args.symbol or sorted(pc.unique(pq.read_table(args.data / f'ord_{date}.parquet', columns=['Symbol'])['Symbol']).to_pylist())
    rows, checks, failures = [], [], []
    args.out.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        try:
            row, sampled = audit_stock(args.data, symbol, args.stride)
            rows.append(row)
            checks.extend(sampled)
            print(f"{symbol}: events={row['events']:,}, close={row['close_volume_equal']}, "
                  f"strict/same-second/diagnostic={row['strict_matches']}/{row['same_second_matches']}/{row['diagnostic_matches']} of {row['snapshots_sampled']}", flush=True)
        except Exception as exc:
            failures.append({'symbol': symbol, 'error': str(exc)})
            print(f'{symbol}: FAILED {exc}', flush=True)
    keys = ['events', 'adds', 'cancels', 'executions', 'status_events', 'anomalies',
            'expected_pre_add_active_legs', 'excluded_after_hours_trades',
            'snapshots_sampled', 'strict_matches', 'same_second_matches', 'diagnostic_matches']
    summary = {'date': date, 'requested_symbols': len(symbols), 'successful_symbols': len(rows),
               'failures': failures, **{k: sum(r.get(k, 0) for r in rows) for k in keys},
               'close_volume_matches': sum(r['close_volume_equal'] for r in rows),
               'close_top10_matches': sum(r['close_top10_equal'] for r in rows),
               'seconds': round(time.perf_counter() - start, 3),
               'sample_rule': f'every {args.stride}th continuous snapshot per stock, excluding 09:30, 11:30 and 13:00 boundaries',
               'alignment': 'strict <= label; primary search [label,label+999ms]; extra diagnostic preceding 2000ms',
               'no_snapshot_overwrite': True,
               'stocks': rows}
    summary['input_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in args.data.glob('*.parquet')}
    (args.out / 'audit_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, data in [('audit_stocks.csv', rows), ('audit_snapshot_samples.csv', checks)]:
        if data:
            with (args.out / name).open('w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(data[0]))
                writer.writeheader()
                writer.writerows(data)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('stocks', 'input_sha256')}, ensure_ascii=False), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
