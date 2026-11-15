#!/usr/bin/env python3
"""Local web dashboard for the Ethernet link monitor.

Same contract as the terminal dashboards: a viewer that reads the running
monitor's snapshot and never stops it. Closing the page or this process changes
nothing about monitoring or Telegram.

The server is deliberately small and standard library only:

* it binds loopback, so it is reachable from this machine and nowhere else;
* it answers GET for four known paths and a JSON snapshot, nothing more;
* it never lists a directory, accepts an upload, or serves a file by request name;
* it loads no external asset, so the page works at a site without internet.

Reach it from another machine through an SSH tunnel, never by widening the bind:

    ssh -L 8765:127.0.0.1:8765 tower-host
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import monitor_runtime as runtime
import monitor_view

DEFAULT_PORT = 8765
LOOPBACK = '127.0.0.1'
WEB_ROOT = Path(__file__).with_name('web')
CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                 '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml'}
# No external origin can ever be reached: the policy allows only this server.
POLICY = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
          "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# Explicit allowlist: a request name is a key here, never a path on disk.
ASSETS = {'/': 'index.html', '/index.html': 'index.html', '/styles.css': 'styles.css',
          '/app.js': 'app.js', '/favicon.svg': 'favicon.svg'}


class Handler(BaseHTTPRequestHandler):
    """Serves the panel and one read-only JSON snapshot."""

    server_version = 'ethernet-monitor-web'
    sys_version = ''
    client = None
    interval = 60

    def log_message(self, *args):
        """A local viewer should not fill the terminal that started it with access logs."""

    def reply(self, status, body, content_type, extra=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', POLICY)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def version_string(self):
        """No empty trailing field from the unused Python version."""
        return self.server_version

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/snapshot':
            view = monitor_view.describe(self.client.snapshot(), self.interval)
            body = json.dumps(monitor_view.snapshot_payload(view)).encode('utf-8')
            return self.reply(200, body, 'application/json; charset=utf-8')
        name = ASSETS.get(path)
        if name is None:
            return self.reply(404, b'not found\n', 'text/plain; charset=utf-8')
        try:
            body = (WEB_ROOT / name).read_bytes()
        except OSError:
            return self.reply(500, b'asset missing\n', 'text/plain; charset=utf-8')
        return self.reply(200, body, CONTENT_TYPES[Path(name).suffix])

    def do_POST(self):
        self.reject()

    def do_PUT(self):
        self.reject()

    def do_PATCH(self):
        self.reject()

    def do_DELETE(self):
        self.reject()

    def do_HEAD(self):
        self.reject()

    def __getattr__(self, name):
        """Refuse every other verb (OPTIONS, TRACE, CONNECT, anything future) the same way.

        BaseHTTPRequestHandler looks up `do_<METHOD>` and falls back to its own 501 when the
        lookup misses; that fallback carries neither the CSP nor `no-store`, so the refusal is
        answered here instead.
        """
        if name.startswith('do_'):
            return self.reject
        raise AttributeError(name)

    def reject(self):
        """This viewer only reads: everything else is refused with an accurate Allow header.

        No body: a HEAD refusal must not carry one, and the status plus Allow say enough.
        """
        self.reply(405, b'', 'text/plain; charset=utf-8', [('Allow', 'GET')])


def make_server(port, client, interval=60):
    """Bind the panel to loopback. Port 0 asks the OS for a free port."""
    handler = type('BoundHandler', (Handler,), {'client': client, 'interval': interval})
    server = ThreadingHTTPServer((LOOPBACK, port), handler)
    server.daemon_threads = True
    return server


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Local web dashboard for the Ethernet link monitor.',
        # Abbreviations would silently turn a mistaken --host into --hosts and start a monitor
        # with a nonsense inventory instead of refusing the option.
        allow_abbrev=False,
        epilog='It is a viewer: closing it never stops monitoring. It binds 127.0.0.1 only, so '
               'reach it from another machine with an SSH tunnel, not by widening the bind.')
    parser.add_argument('--demo', action='store_true', help='synthetic data; no network, credentials or Telegram')
    parser.add_argument('--hosts', default='hosts.csv', help='editable host CSV (default: hosts.csv)')
    parser.add_argument('--interval', type=float, default=60,
                        help='scan interval used when this dashboard starts the monitor (default: 60)')
    parser.add_argument('--runtime-dir', default=monitor_view.default_runtime_dir(),
                        help='private directory for local monitor IPC')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'loopback port to listen on (default: {DEFAULT_PORT}); 0 picks a free port')
    args = parser.parse_args(argv)
    if not 0 < args.interval < float('inf'):
        parser.error('interval must be positive and finite')
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    return args


def main(argv=None):
    args = monitor_view.demo_interval(parse_args(argv))
    client = monitor_view.MonitorClient(args)
    try:
        client.attach()
    except (runtime.RuntimeErrorLocal, OSError) as error:
        # Serve anyway: the page states that no monitor is running instead of hiding it.
        print(f'warning: {error}', file=sys.stderr)
        print('the page will show the monitor as disconnected until one is running', file=sys.stderr)
    try:
        server = make_server(args.port, client, args.interval)
    except OSError:
        print(f'error: port {args.port} is not available; choose another with --port', file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    print(f'Ethernet monitor web dashboard: http://{host}:{port}/')
    print('A viewer only: closing it never stops monitoring. Ctrl+C to close.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
