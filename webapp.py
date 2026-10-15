"""Loopback-only teaching terminal; standard library HTTP server, no broker connection."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import uuid
from urllib.parse import urlparse

from orderbook import OrderBook, cents, parse_time
from simulator import HistoricalSession

ROOT = Path(__file__).resolve().parent
SESSIONS = {}
LOCK = threading.RLock()
DATA = ROOT / 'sse50_20260923'


class Handler(BaseHTTPRequestHandler):
    def respond(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health':
            return self.respond({'app': 'sse50-workbench'})
        if path == '/api/symbols':
            import pyarrow.parquet as pq
            import pyarrow.compute as pc
            from orderbook import dataset_date
            symbols = sorted(pc.unique(pq.read_table(DATA / f'ord_{dataset_date(DATA)}.parquet', columns=['Symbol'])['Symbol']).to_pylist())
            return self.respond({'symbols': symbols})
        if path == '/api/audit':
            file = ROOT / 'reports' / 'audit_summary.json'
            result = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
            auctions = ROOT / 'reports' / 'auction_validation.json'
            if auctions.exists():
                result['auction'] = json.loads(auctions.read_text(encoding='utf-8'))
            return self.respond(result)
        files = {'/': (ROOT / 'web/index.html', 'text/html; charset=utf-8')}
        if path not in files or not files[path][0].exists():
            return self.respond({'error': '页面不存在'}, 404)
        file, mime = files[path]
        content = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('请求大小无效')
            request = json.loads(self.rfile.read(size))
            path = urlparse(self.path).path
            with LOCK:
                if path == '/api/reset':
                    session = HistoricalSession.from_data(DATA, int(request.get('symbol', 601688)))
                    SESSIONS.pop(request.get('sid'), None)
                    # Browser sessions are isolated and capped to avoid unbounded memory.
                    if len(SESSIONS) >= 8:
                        SESSIONS.pop(next(iter(SESSIONS)))
                    sid = uuid.uuid4().hex
                    SESSIONS[sid] = session
                    return self.respond({'sid': sid, **session.state()})
                if path == '/api/auction':
                    book = OrderBook(int(request.get('symbol', 600000)))
                    rows = request.get('orders', [])
                    if not 1 <= len(rows) <= 100:
                        raise ValueError('集合竞价演示须有1–100笔限价单')
                    for i, row in enumerate(rows):
                        qty = row['quantity']
                        if type(qty) is not int or qty <= 0:
                            raise ValueError('数量须为正整数')
                        book.add_resting(str(row.get('id', i+1)), row['side'], cents(row['price']), qty, 91500000+i)
                    before = book.snapshot(92459000)
                    result = book.auction(92500000)
                    return self.respond({'before': before, **result, 'after': book.snapshot(92500000)})
                session = SESSIONS.get(request.get('sid'))
                if session is None:
                    raise ValueError('请先重置并加载一只股票')
                if path == '/api/advance':
                    result = session.advance(parse_time(request['time']))
                elif path == '/api/order':
                    result = session.submit(request['side'], request['kind'], request['quantity'], request.get('price'), request.get('protection'))
                elif path == '/api/cancel':
                    result = session.cancel()
                else:
                    return self.respond({'error': '接口不存在'}, 404)
                return self.respond(result)
        except (ValueError, KeyError, TypeError) as exc:
            self.respond({'error': str(exc)}, 400)
        except Exception as exc:
            self.log_error('internal error: %s', exc)
            self.respond({'error': f'操作失败: {type(exc).__name__}: {exc}'}, 500)

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(('4', '5')):
            super().log_message(fmt, *args)


def main():
    global DATA
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=DATA)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    DATA = args.data.resolve()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Local: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
