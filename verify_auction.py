"""Generate auction trades from pre-auction orders, then independently compare tape."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import pyarrow.compute as pc
import pyarrow.parquet as pq
from orderbook import OrderBook, apply_market_event, cents, dataset_date, load_stock_events


def clone(book):
    branch = OrderBook(book.symbol)
    for oid, r in book.orders.items():
        branch.add_resting(oid, r.side, r.price, r.remaining, r.entered_at)
    return branch


def main():
    data = Path(__file__).resolve().parent / 'sse50_20260923'
    date = dataset_date(data)
    symbols = sorted(pc.unique(pq.read_table(data / f'ord_{date}.parquet', columns=['Symbol'])['Symbol']).to_pylist())
    rows = []
    for symbol in symbols:
        events, _ = load_stock_events(data, symbol)
        book = OrderBook(symbol, retain_trades=False)
        stats = defaultdict(int)
        results = {}
        expected = {92500000: Counter(), 150000000: Counter()}
        for event in events:
            time, channel, seq, kind, row = event
            if kind == 'exe' and row['BSFlag'] == 0 and time in expected:
                if time not in results:
                    results[time] = clone(book).auction(time)
                expected[time][(f"{channel}:{row['BidOrder']}", f"{channel}:{row['AskOrder']}", cents(round(float(row['Price']), 2)))] += int(row['Volume'])
            apply_market_event(book, event, stats)
        for at, label in [(92500000, 'open'), (150000000, 'close')]:
            actual = Counter()
            result = results.get(at, {'price': None, 'volume': 0, 'trades': []})
            for t in result['trades']:
                actual[(t['buy_order_id'], t['sell_order_id'], cents(t['price']))] += t['volume']
            rows.append({'symbol': symbol, 'stage': label, 'generated_volume': result['volume'],
                         'reference_volume': sum(expected[at].values()), 'pair_price_volume_equal': actual == expected[at],
                         'generated_price': result['price'], 'generated_fills': len(result['trades'])})
        print(symbol, [(r['stage'], r['pair_price_volume_equal']) for r in rows[-2:]], flush=True)
    summary = {'stocks': len(symbols), 'opening_matches': sum(r['pair_price_volume_equal'] for r in rows if r['stage'] == 'open'),
               'closing_matches': sum(r['pair_price_volume_equal'] for r in rows if r['stage'] == 'close'),
               'method': 'freeze historical book before first auction execution; matching consumes orders only; reference tape used only for subsequent comparison',
               'stocks_results': rows}
    target = Path(__file__).resolve().parent / 'reports/auction_validation.json'
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'stocks_results'}), flush=True)
    if not all(r['pair_price_volume_equal'] for r in rows):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
