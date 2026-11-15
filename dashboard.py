#!/usr/bin/env python3
"""Live Ethernet link dashboard built with Textual.

The dashboard is a viewer: it attaches to the detached monitor, or starts one
when nothing is running, and then polls the private local snapshot. Closing it
never stops monitoring or Telegram alerts; only the explicit `s` binding does.
"""

import argparse
from datetime import datetime
import sys
import time


def missing_dependency(error):
    """Explain the install step instead of a bare ModuleNotFoundError traceback."""
    print(f'dashboard.py needs {error.name!r}, which is not installed.', file=sys.stderr)
    print('install the dependencies with:', file=sys.stderr)
    print('  uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt', file=sys.stderr)
    print('the dependency-free viewer is: python3 monitor.py', file=sys.stderr)


try:
    import textual  # noqa: F401  (checked here so the message below is actionable)
except ModuleNotFoundError as error:
    if __name__ == '__main__':
        missing_dependency(error)
        raise SystemExit(2) from None
    raise

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, RichLog, Static

import monitor_runtime as runtime
# The view model, status order and viewer attach live in monitor_view so the web dashboard can
# read the monitor without importing the TUI stack. They stay importable from here on purpose.
from monitor_view import (AGE_GRACE, MIN_STALE, STATUS_ORDER, MonitorClient,  # noqa: F401
                          default_runtime_dir, demo_interval, describe, failure_reason,
                          format_speed, sort_records, summarize, summary_text)

POLL_SECONDS = 0.5
STATUS_STYLE = {
    'DOWN': 'bold #ff5f5f',
    'LOW': 'bold #ffaf00',
    'OK': 'bold #5fd787',
    'UNREACHABLE': '#8a8a8a',
    'UNKNOWN': '#8a8a8a',
    'PENDING': 'italic #6c6c6c',
}


def summary_markup(counts):
    parts = [f'[{STATUS_STYLE.get(status, "grey62")}]{status} {counts[status]}[/]'
             for status in STATUS_ORDER if counts.get(status)]
    parts += [f'[{STATUS_STYLE.get(status, "grey62")}]{status} {count}'
              for status, count in sorted(counts.items()) if status not in STATUS_ORDER]
    return '  '.join(parts) if parts else '[grey62]no hosts configured[/]'


def text_report(view, now=None):
    """Printable snapshot for terminals without curses or Textual support."""
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(view['updated']).strftime('%H:%M:%S') if view['updated'] else '-'
    lines = [f"ETHERNET MONITOR | {'DEMO - NO REAL ALERTS' if view['demo'] else 'LIVE'} | {view['state'].upper()}",
             f"Scans: {view['scans']}  Last: {when}  PID: {view['pid'] or '-'}"]
    lines.append(f'{"STATUS":<12} {"NAME":<28} {"IP":<16} {"PORT":<6} {"SPEED":<7} MIN')
    for record in sort_records(view['records']):
        lines.append(f'{record.get("status", "UNKNOWN"):<12} {(record.get("name") or "")[:28]:<28} '
                     f'{record.get("ip") or "":<16} {str(record.get("index") or "-"):<6} '
                     f'{format_speed(record.get("speed")):<7} {format_speed(record.get("minimum"))}')
    if view['records']:
        lines.append(summary_text(summarize(view['records'])))
    elif view['state'] != 'outdated':
        lines.append('No hosts yet: the monitor publishes its first scan shortly.')
    if view['state'] == 'disconnected':
        lines.append('Monitor is not running; start it with monitor.py or dashboard.py --demo.')
    elif view['state'] == 'outdated':
        lines.append('This monitor was started by an older version and publishes no per-host rows; stop it and start it again.')
    elif view['state'] == 'stale':
        lines.append('Snapshot is stale; the monitor stopped completing scans.')
    lines.extend(view['notices'])
    return lines


class Dashboard(App):
    """Textual dashboard over the monitor snapshot."""

    TITLE = 'Ethernet link monitor'
    SUB_TITLE = 'viewer only: closing it does not stop monitoring'
    CSS = """
    #summary { height: auto; padding: 0 1; }
    #body { height: 1fr; }
    #hosts { width: 3fr; border: round #303030; }
    #side { width: 2fr; }
    #detail { height: auto; padding: 0 1; border: round #303030; }
    #alerts { height: 1fr; border: round #303030; }
    """
    BINDINGS = [
        Binding('q', 'quit', 'Quit'),
        Binding('r', 'refresh', 'Refresh'),
        Binding('s', 'stop', 'Stop monitor'),
        Binding('o', 'toggle_order', 'Sort'),
    ]
    HINTS = {
        'outdated': 'This monitor was started by an older version and publishes no per-host rows. '
                    'Press s to stop it, then start it again.',
        'starting': 'Waiting for the first completed scan...',
        'disconnected': 'No monitor running. Press r to retry, or start one with monitor.py.',
        'stale': 'The monitor stopped completing scans; the table shows its last published snapshot.',
    }

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.client = MonitorClient(args)
        self.worst_first = True
        self.view = describe(None, args.interval)
        self.error = ''
        self.selected = None
        self.poll_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id='summary')
        with Horizontal(id='body'):
            with Vertical():
                yield DataTable(id='hosts', zebra_stripes=True, cursor_type='row')
            with Vertical(id='side'):
                yield Static(id='detail')
                yield RichLog(id='alerts', wrap=True, markup=True)
        yield Footer()

    def on_mount(self):
        table = self.query_one('#hosts', DataTable)
        for label in ('Status', 'Host', 'IP', 'Port', 'Speed', 'Min'):
            table.add_column(label, key=label.lower())
        try:
            snapshot = self.client.attach()
        except (runtime.RuntimeErrorLocal, OSError) as error:
            self.error = str(error)
            snapshot = None
        self.apply(snapshot)
        self.poll_timer = self.set_interval(POLL_SECONDS, self.refresh_snapshot)

    def refresh_snapshot(self):
        self._collect()

    @work(thread=True, exclusive=True, group='snapshot')
    def _collect(self):
        snapshot = self.client.snapshot()
        self.call_from_thread(self.apply, snapshot)

    def apply(self, snapshot):
        """Render one snapshot; called from the poll worker and from tests.

        A poll can land after the app unmounted its widgets — quitting with `q` in the middle
        of a scan is enough. There is nothing left to repaint then, and a late snapshot is
        dropped instead of raising inside the worker thread.
        """
        self.view = describe(snapshot, self.args.interval)
        try:
            self._render(self.view)
        except NoMatches:
            pass

    def _render(self, view):
        state = view['state']
        badge = 'DEMO' if view['demo'] else 'LIVE'
        header = Text()
        header.append(f'{badge} ', style='bold reverse')
        header.append('  ')
        if state != 'live':
            # A demo monitor can also go stale or disappear; say so instead of hiding it.
            header.append(f'{state.upper()} ', style='bold reverse')
            header.append('  ')
        header.append_text(Text.from_markup(summary_markup(summarize(view['records']))))
        header.append('\n')
        when = datetime.fromtimestamp(view['updated']).strftime('%H:%M:%S') if view['updated'] else '-'
        details = [f'Scans {view["scans"]}', f'last {when}', f'pid {view["pid"] or "-"}']
        if view['state'] == 'stale' and view['age'] is not None:
            details.append(f'STALE {int(view["age"])}s')
        if self.error:
            details.append(f'ERROR {self.error}')
        elif view['state'] == 'disconnected':
            details.append('MONITOR NOT RUNNING - press s to stop, q to quit')
        elif view['state'] == 'outdated':
            details.append('MONITOR FROM AN OLDER VERSION - press s, then start it again')
        elif view['state'] == 'starting':
            details.append('waiting for the first scan')
        header.append('  |  '.join(details), style='grey62')
        self.query_one('#summary', Static).update(header)
        self._render_table(view)
        self._render_detail(view)
        self._render_alerts(view)

    def _render_table(self, view):
        table = self.query_one('#hosts', DataTable)
        row = min(table.cursor_row, max(0, len(view['records']) - 1))
        table.clear()
        for record in sort_records(view['records'], self.worst_first):
            status = record.get('status') or 'UNKNOWN'
            table.add_row(
                Text(status, style=STATUS_STYLE.get(status, 'grey62')),
                record.get('name') or '-',
                record.get('ip') or '-',
                str(record.get('index') or '-'),
                format_speed(record.get('speed')),
                format_speed(record.get('minimum')),
                key=f'{record.get("ip")}:{record.get("index")}',
            )
        if view['records']:
            table.move_cursor(row=min(row, len(view['records']) - 1))

    def _render_detail(self, view):
        record = self.selected
        if record is None and view['records']:
            record = sort_records(view['records'])[0]
        panel = self.query_one('#detail', Static)
        if record is None:
            panel.update(Text('No host selected', style='grey62'))
            return
        status = record.get('status') or 'UNKNOWN'
        body = Text()
        body.append(f'{record.get("name") or "-"}\n', style='bold')
        body.append(f'{status}', style=STATUS_STYLE.get(status, 'grey62'))
        body.append(f'  {format_speed(record.get("speed"))} / min {format_speed(record.get("minimum"))}\n', style='grey62')
        body.append(f'{record.get("ip") or "-"}  port {record.get("index") or "-"}\n', style='grey62')
        if record.get('detail'):
            body.append(str(record['detail']), style='italic grey62')
        panel.update(body)

    def _render_alerts(self, view):
        log = self.query_one('#alerts', RichLog)
        log.clear()
        for notice in view['notices'][-3:]:
            log.write(f'[grey62]{notice}[/]')
        if not view['notices']:
            # An empty panel reads as a broken screen; say what is actually going on.
            hint = self.HINTS.get(view['state'], 'No alerts since this monitor started.')
            log.write(f'[grey50]{hint}[/]')

    def on_data_table_row_highlighted(self, event):
        records = sort_records(self.view['records'], self.worst_first)
        self.selected = records[event.cursor_row] if 0 <= event.cursor_row < len(records) else None
        self._render_detail(self.view)

    def action_refresh(self):
        self.error = ''
        self._collect()

    def action_toggle_order(self):
        self.worst_first = not self.worst_first
        self.notify('Worst links first' if self.worst_first else 'Inventory order')
        self._render(self.view)

    def action_stop(self):
        if self.view['state'] == 'disconnected' and not self.client.path:
            self.notify('Monitor is not running', severity='warning')
            return
        if self.client.stop():
            self.notify('Monitor stopped; alerts are off until it is started again')
        else:
            self.notify('Could not stop the monitor; check monitor.log', severity='error')
        self.apply(self.client.snapshot())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Live Textual dashboard for the Ethernet link monitor.',
                                     epilog='Closing the dashboard never stops monitoring. Usage with a running monitor: '
                                            'start it with monitor.py or click s here to stop it.')
    parser.add_argument('--demo', action='store_true', help='synthetic data; no network, credentials or Telegram')
    parser.add_argument('--hosts', default='hosts.csv', help='editable host CSV (default: hosts.csv)')
    parser.add_argument('--interval', type=float, default=60, help='scan interval used when this dashboard starts the monitor (default: 60)')
    parser.add_argument('--runtime-dir', default=default_runtime_dir(),
                        help='private directory for local monitor IPC')
    parser.add_argument('--once', action='store_true', help='print one snapshot and exit; starts and stops its own monitor when needed')
    args = parser.parse_args(argv)
    if not 0 < args.interval < float('inf'):
        parser.error('interval must be positive and finite')
    return args


def main(argv=None):
    args = demo_interval(parse_args(argv))
    if args.once:
        client = MonitorClient(args)
        try:
            client.attach()
        except (runtime.RuntimeErrorLocal, OSError) as error:
            print(f'Error: {error}', file=sys.stderr)
            return 2
        try:
            print('\n'.join(text_report(describe(client.first_snapshot(), args.interval))))
        finally:
            if client.started:
                client.stop()
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('dashboard needs a terminal; use --once for a printable snapshot', file=sys.stderr)
        return 2
    Dashboard(args).run()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
