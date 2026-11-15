"""Snapshot view model and viewer-side attach, shared by every viewer.

The monitor publishes one JSON snapshot over its private socket. This module owns
the two things every viewer needs and nothing that belongs to one of them: how a
snapshot becomes a view model, and how a viewer attaches to a running monitor (or
starts one).

It imports no terminal, HTTP or Textual stack, so the web dashboard can read the
monitor without requiring the TUI dependencies, and the TUI can keep rendering
exactly the same model it always did.
"""

import os
import time
from pathlib import Path

import monitor_runtime as runtime

STATUS_ORDER = ('DOWN', 'LOW', 'UNREACHABLE', 'UNKNOWN', 'PENDING', 'OK')
OUT_OF_SPEC = ('DOWN', 'LOW')
NO_DATA = ('UNREACHABLE', 'UNKNOWN', 'PENDING')
MIN_STALE = 30
AGE_GRACE = 20


def default_runtime_dir():
    """The private IPC directory every viewer uses when none is given."""
    return os.path.join(os.environ.get('XDG_RUNTIME_DIR', f'/tmp/ethernet-monitor-{os.getuid()}'),
                        'ethernet-monitor')


def demo_interval(args):
    """The demo rotates faster than the production default so it can be seen."""
    if args.demo and args.interval == 60:
        args.interval = 3
    return args


def format_speed(speed):
    """Render Mbps the way operators read link speeds."""
    if speed is None:
        return '-'
    if speed >= 1000:
        return f'{speed / 1000:.1f}G'
    return f'{speed:g}M'


def summarize(records):
    """Count hosts per status; unknown statuses are reported as they arrive."""
    counts = {}
    for record in records:
        status = record.get('status') or 'UNKNOWN'
        counts[status] = counts.get(status, 0) + 1
    return counts


def summary_text(counts):
    parts = [f'{status} {counts[status]}' for status in STATUS_ORDER if counts.get(status)]
    parts += [f'{status} {count}' for status, count in sorted(counts.items()) if status not in STATUS_ORDER]
    return '  '.join(parts) if parts else 'no hosts configured'


def sort_records(records, worst_first=True):
    """Order by severity so the noisy links surface first."""
    if not worst_first:
        return list(records)

    def key(record):
        status = record.get('status') or 'UNKNOWN'
        rank = STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)
        return rank, record.get('name') or ''

    return sorted(records, key=key)


def describe(snapshot, interval=None, now=None):
    """Turn one monitor snapshot into the state the viewers render."""
    now = time.time() if now is None else now
    if not snapshot:
        return {'state': 'disconnected', 'records': [], 'notices': [], 'scans': 0, 'updated': 0,
                'pid': None, 'demo': False, 'age': None, 'hosts': '', 'interval': interval}
    identity = snapshot.get('identity') or {}
    span = identity.get('interval') or interval or 60
    updated = snapshot.get('updated') or 0
    age = now - updated if updated else None
    records = list(snapshot.get('records') or ())
    if 'records' not in snapshot:
        # A monitor started by an older version publishes only rendered lines.
        state = 'outdated'
    elif not records or age is None:
        # No records yet, or no publish time: never claim freshness we cannot prove.
        state = 'starting'
    elif age > max(MIN_STALE, span + AGE_GRACE):
        state = 'stale'
    else:
        state = 'live'
    return {'state': state, 'records': records,
            'notices': list(snapshot.get('notices') or ()),
            'scans': snapshot.get('scans') or 0, 'updated': updated, 'age': age,
            'pid': snapshot.get('pid'), 'demo': bool(identity.get('demo')),
            'hosts': identity.get('hosts') or '', 'interval': span}


def tone(status):
    """Severity a viewer paints. Anything the model does not recognise is never 'ok'."""
    if status in OUT_OF_SPEC:
        return 'alarm'
    return 'ok' if status == 'OK' else 'nodata'


def snapshot_payload(view, now=None):
    """Shape one view model for a browser: worst-first rows plus the verdict facts.

    Classification lives here and not in the page, so every viewer counts severity the same way
    and a status this code has never seen can never be painted as healthy.
    """
    now = time.time() if now is None else now
    links = []
    for record in sort_records(view['records']):
        status = record.get('status') or 'UNKNOWN'
        links.append({'name': record.get('name') or '', 'ip': record.get('ip') or '',
                      'index': record.get('index'), 'status': status, 'tone': tone(status),
                      'detail': record.get('detail') or '',
                      'speed_text': format_speed(record.get('speed')),
                      'minimum_text': format_speed(record.get('minimum'))})
    verdict = {'total': len(links), 'out_of_spec': 0, 'no_data': 0, 'ok': 0}
    for link in links:
        if link['tone'] == 'alarm':
            verdict['out_of_spec'] += 1
        elif link['tone'] == 'ok':
            verdict['ok'] += 1
        else:
            verdict['no_data'] += 1
    return {'generated': now, 'state': view['state'], 'demo': view['demo'], 'scans': view['scans'],
            'updated': view['updated'], 'age': view['age'], 'interval': view['interval'],
            # The browser gets the file name, never the operator's absolute path.
            'source': Path(view['hosts']).name if view.get('hosts') else '',
            'notices': list(view['notices'])[-3:], 'counts': summarize(view['records']),
            'verdict': verdict, 'links': links}


def failure_reason(path):
    """Surface the monitor's own last error line instead of only 'failed to start'."""
    try:
        log = (path / 'monitor.log').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return 'no monitor.log yet'
    for line in reversed(log.strip().splitlines()):
        if ': error: ' in line:
            return line.split(': error: ', 1)[1].strip()
    return 'no error line in monitor.log'


class MonitorClient:
    """Minimal viewer-side access to the monitor's private local snapshot."""

    def __init__(self, args):
        self.args = args
        self.path = None
        self.started = False

    def attach(self):
        self.path = runtime.directory(self.args.runtime_dir)
        snapshot = runtime.request(self.path, 'status')
        if snapshot:
            return snapshot
        try:
            runtime.start(self.path, self.args)
        except runtime.RuntimeErrorLocal as error:
            raise runtime.RuntimeErrorLocal(f'{error}: {failure_reason(self.path)}') from None
        self.started = True
        return runtime.request(self.path, 'status')

    def snapshot(self):
        if self.path is None:
            return None
        try:
            return runtime.request(self.path, 'status')
        except runtime.RuntimeErrorLocal:
            return None

    def first_snapshot(self, deadline=15.0, step=0.15):
        """Wait for the first published scan so a one-shot report is not empty."""
        snapshot = self.snapshot()
        end = time.monotonic() + deadline
        while (snapshot is not None and 'records' in snapshot and not snapshot.get('records')
               and time.monotonic() < end):
            time.sleep(step)
            snapshot = self.snapshot()
        return snapshot

    def stop(self):
        if self.path is None:
            return False
        try:
            runtime.request(self.path, 'stop')
            runtime.wait_stopped(self.path)
        except runtime.RuntimeErrorLocal:
            return False
        return True
