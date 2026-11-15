import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import dashboard


def snapshot(records, updated=None, interval=60, demo=False):
    return {'pid': 4242, 'identity': {'hosts': '/tmp/hosts.csv', 'demo': demo, 'interval': interval},
            'scans': 7, 'updated': time.time() if updated is None else updated,
            'records': records, 'notices': ['ALERT: port down'], 'lines': ['ETHERNET MONITOR | LIVE']}


def record(name='Link', ip='192.0.2.1', index=1, speed=1000, minimum=1000, status='OK', detail=''):
    return {'name': name, 'ip': ip, 'index': index, 'speed': speed, 'minimum': minimum,
            'status': status, 'detail': detail}


class HelperTests(unittest.TestCase):
    def test_format_speed(self):
        self.assertEqual(dashboard.format_speed(None), '-')
        self.assertEqual(dashboard.format_speed(1000), '1.0G')
        self.assertEqual(dashboard.format_speed(100), '100M')
        self.assertEqual(dashboard.format_speed(2.5), '2.5M')

    def test_summarize_and_sort(self):
        records = [record('A', '192.0.2.1', status='OK'), record('B', '192.0.2.2', status='DOWN'),
                   record('C', '192.0.2.3', status='LOW'), record('D', '192.0.2.4', status='DOWN')]
        self.assertEqual(dashboard.summarize(records), {'OK': 1, 'DOWN': 2, 'LOW': 1})
        self.assertEqual([item['name'] for item in dashboard.sort_records(records)], ['B', 'D', 'C', 'A'])
        self.assertEqual([item['name'] for item in dashboard.sort_records(records, False)], ['A', 'B', 'C', 'D'])
        self.assertEqual(dashboard.summary_text({'OK': 2, 'DOWN': 1}), 'DOWN 1  OK 2')

    def test_describe_states_and_staleness(self):
        now = time.time()
        disconnected = dashboard.describe(None)
        self.assertEqual((disconnected['state'], disconnected['records']), ('disconnected', []))
        starting = dashboard.describe(snapshot([], updated=now, interval=60))
        self.assertEqual(starting['state'], 'starting')
        live = dashboard.describe(snapshot([record()], updated=now, interval=60))
        self.assertEqual(live['state'], 'live')
        # The monitor's own interval, not the dashboard default, decides what is stale.
        stale = dashboard.describe(snapshot([record()], updated=now - 120, interval=60))
        self.assertEqual(stale['state'], 'stale')
        self.assertGreater(stale['age'], 100)
        fresh = dashboard.describe(snapshot([record()], updated=now - 120, interval=600))
        self.assertEqual(fresh['state'], 'live')

    def test_describe_reports_identity_and_notices(self):
        view = dashboard.describe(snapshot([record()], demo=True))
        self.assertTrue(view['demo'])
        self.assertEqual(view['notices'], ['ALERT: port down'])
        self.assertEqual(view['scans'], 7)
        self.assertEqual(view['pid'], 4242)

    def test_describe_flags_a_monitor_started_by_an_older_version(self):
        legacy = {'pid': 1, 'identity': {'hosts': '/tmp/h', 'demo': False, 'interval': 60},
                  'scans': 915, 'updated': time.time(), 'lines': ['ETHERNET MONITOR | LIVE']}
        view = dashboard.describe(legacy)
        # Never report 'waiting for the first scan' when the monitor simply cannot publish rows.
        self.assertEqual(view['state'], 'outdated')
        self.assertEqual(view['records'], [])
        self.assertIn('older version', ' '.join(dashboard.text_report(view)))

    def test_text_report_matches_table(self):
        lines = dashboard.text_report(dashboard.describe(snapshot([record(status='DOWN', speed=None, detail='port down')])))
        self.assertIn('ETHERNET MONITOR | LIVE | LIVE', lines[0])
        self.assertIn('DOWN', ' '.join(lines))
        self.assertIn('ALERT: port down', lines)


class FakeClient:
    """Stands in for the IPC client so rendering is tested without a monitor."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.path = None
        self.started = False

    def attach(self):
        return self.snapshots[0]

    def snapshot(self):
        return self.snapshots[-1]

    def stop(self):
        return True


class RenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_follows_snapshots_and_order(self):
        first = snapshot([record('Alpha', '192.0.2.1', status='OK'),
                          record('Beta', '192.0.2.2', status='DOWN', speed=None, detail='port down')])
        second = snapshot([record('Alpha', '192.0.2.1', status='OK'),
                           record('Beta', '192.0.2.2', status='DOWN', speed=None, detail='port down'),
                           record('Gamma', '192.0.2.3', status='LOW', speed=100, minimum=1000)])
        args = dashboard.demo_interval(dashboard.parse_args(['--demo']))
        app = dashboard.Dashboard(args)
        app.client = FakeClient([first, second])
        async with app.run_test(size=(120, 40)) as pilot:
            # These assertions are about painting, not about the clock: the interval poll runs
            # on its own test, so here it is paused instead of racing the snapshots below.
            app.poll_timer.pause()
            await pilot.pause()
            table = app.query_one('#hosts', dashboard.DataTable)
            self.assertEqual(table.row_count, 2)
            self.assertEqual([str(cell) for cell in table.get_row_at(0)], ['DOWN', 'Beta', '192.0.2.2', '1', '-', '1.0G'])
            # A newer snapshot must repaint without waiting for a key press.
            app.client = FakeClient([second])
            app.apply(second)
            await pilot.pause()
            self.assertEqual(table.row_count, 3)
            self.assertEqual([str(cell) for cell in table.get_row_at(0)][1], 'Beta')
            # Inventory order is available on demand.
            app.action_toggle_order()
            await pilot.pause()
            self.assertEqual([str(cell) for cell in table.get_row_at(0)][1], 'Alpha')
            # A disconnected monitor never keeps stale rows on screen. The fake client moves with
            # the test so the 0.5 s poll cannot re-apply a live snapshot behind the assertion.
            app.client = FakeClient([None])
            app.apply(None)
            await pilot.pause()
            self.assertEqual(table.row_count, 0)
            self.assertEqual(app.view['state'], 'disconnected')

    async def test_a_late_snapshot_after_teardown_is_dropped(self):
        args = dashboard.demo_interval(dashboard.parse_args(['--demo']))
        app = dashboard.Dashboard(args)
        app.client = FakeClient([snapshot([record()])])
        async with app.run_test(size=(120, 40)) as pilot:
            app.poll_timer.pause()
            await pilot.pause()
        # Quitting with q in the middle of a scan lets a poll land after the widgets are gone.
        # That repaint must be dropped, not raised inside the worker thread.
        app.apply(snapshot([record(status='DOWN')], updated=time.time() - 900, interval=3))
        self.assertEqual(app.view['state'], 'stale')

    async def test_badge_comes_from_the_monitor_identity(self):
        args = dashboard.parse_args(['--once'])
        self.assertFalse(args.demo)
        app = dashboard.Dashboard(dashboard.demo_interval(args))
        app.client = FakeClient([snapshot([record()], demo=True)])
        async with app.run_test(size=(120, 40)) as pilot:
            app.poll_timer.pause()
            await pilot.pause()
            summary = app.query_one('#summary', dashboard.Static).content
            self.assertIn('DEMO', summary.plain)
            # A demo monitor can also stop publishing: that state stays visible.
            app.apply(snapshot([record()], demo=True, updated=time.time() - 900, interval=3))
            await pilot.pause()
            summary = app.query_one('#summary', dashboard.Static).content
            self.assertIn('DEMO', summary.plain)
            self.assertIn('STALE', summary.plain)

    async def test_polling_does_not_block_the_interface(self):
        seen = {}

        class SlowClient(FakeClient):
            def snapshot(self):
                seen['thread'] = threading.current_thread()
                time.sleep(2.0)
                return self.snapshots[-1]

        args = dashboard.demo_interval(dashboard.parse_args(['--demo']))
        app = dashboard.Dashboard(args)
        app.client = SlowClient([snapshot([record()])])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            deadline = time.monotonic() + 10
            while 'thread' not in seen and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            self.assertIn('thread', seen)
            self.assertIsNot(seen['thread'], threading.main_thread())
            started = time.monotonic()
            await pilot.press('o')
            await pilot.pause()
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertFalse(app.worst_first)

    async def test_empty_states_explain_themselves(self):
        legacy = {'pid': 1, 'identity': {'hosts': '/tmp/h', 'demo': False, 'interval': 60},
                  'scans': 915, 'updated': time.time(), 'lines': ['ETHERNET MONITOR | LIVE']}
        args = dashboard.demo_interval(dashboard.parse_args(['--demo']))
        app = dashboard.Dashboard(args)
        app.client = FakeClient([legacy])
        async with app.run_test(size=(120, 40)) as pilot:
            app.poll_timer.pause()
            await pilot.pause()
            table = app.query_one('#hosts', dashboard.DataTable)
            self.assertEqual(table.row_count, 0)
            self.assertEqual(app.view['state'], 'outdated')
            log = app.query_one('#alerts', dashboard.RichLog)
            self.assertTrue(log.lines)
            self.assertIn('older version', log.lines[0].text)
            # A disconnected monitor gets its own hint instead of a blank panel.
            app.apply(None)
            await pilot.pause()
            self.assertIn('No monitor running', log.lines[0].text)

    async def test_startup_error_is_surfaced(self):
        args = dashboard.demo_interval(dashboard.parse_args(['--demo']))
        app = dashboard.Dashboard(args)

        def broken():
            raise dashboard.runtime.RuntimeErrorLocal('monitor failed to start; check configuration and monitor.log: bad csv')

        app.client = FakeClient([None])
        app.client.attach = broken
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIn('bad csv', app.error)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = str(Path(self.tmp.name) / 'runtime')

    def cli(self, *args, timeout=90):
        return subprocess.run([sys.executable, 'dashboard.py', *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)

    def monitor(self, *args, timeout=30):
        return subprocess.run([sys.executable, 'monitor.py', *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)

    def test_once_demo_prints_snapshot_and_leaves_no_monitor(self):
        result = self.cli('--demo', '--once', '--runtime-dir', self.runtime)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DEMO - NO REAL ALERTS', result.stdout)
        for status in ('OK', 'LOW', 'DOWN', 'UNREACHABLE', 'UNKNOWN'):
            self.assertIn(status, result.stdout)
        self.assertEqual(result.stdout.count('Demo link'), 5)
        # A one-shot report must not leave a detached monitor behind.
        status = self.monitor('--demo', '--status', '--runtime-dir', self.runtime)
        self.assertEqual(status.returncode, 2)
        self.assertIn('not running', status.stderr)

    def test_once_attaches_to_existing_monitor_without_stopping_it(self):
        self.addCleanup(self.monitor, '--demo', '--interval', '3', '--stop', '--runtime-dir', self.runtime)
        started = self.monitor('--demo', '--interval', '3', '--runtime-dir', self.runtime)
        self.assertEqual(started.returncode, 0, started.stderr)
        attach = self.cli('--demo', '--once', '--interval', '3', '--runtime-dir', self.runtime)
        self.assertEqual(attach.returncode, 0, attach.stderr)
        self.assertIn('Demo link', attach.stdout)
        # Attaching is only a view: the monitor it attached to keeps running.
        still = self.monitor('--demo', '--status', '--runtime-dir', self.runtime)
        self.assertEqual(still.returncode, 0, still.stderr)
        stopped = self.monitor('--demo', '--interval', '3', '--stop', '--runtime-dir', self.runtime)
        self.assertEqual((stopped.returncode, stopped.stdout), (0, 'Monitor stopped.\n'))

    def test_non_terminal_requires_once(self):
        result = self.cli('--demo', '--runtime-dir', self.runtime)
        self.assertEqual(result.returncode, 2)
        self.assertIn('needs a terminal', result.stderr)

    def test_configuration_error_is_actionable(self):
        hosts = Path(self.tmp.name) / 'hosts.csv'
        hosts.write_text('nombre,ip,modelo,zona,ifindex,min_mbps,community_env\n'
                         'Link,192.0.2.9,AF60,North,,1000,SNMP_COMMUNITY\n', encoding='utf-8')
        result = self.cli('--hosts', str(hosts), '--once', '--runtime-dir', self.runtime)
        self.assertEqual(result.returncode, 2)
        self.assertIn('monitor failed to start', result.stderr)
        self.assertIn('hosts CSV line', result.stderr)

    def test_invalid_interval_is_rejected(self):
        result = self.cli('--demo', '--interval', '0', '--once', '--runtime-dir', self.runtime)
        self.assertEqual(result.returncode, 2)
        self.assertIn('interval must be positive and finite', result.stderr)


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires PTY')
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = str(Path(self.tmp.name) / 'runtime')

    def stop_monitor(self):
        subprocess.run([sys.executable, 'monitor.py', '--demo', '--stop', '--runtime-dir', self.runtime],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)

    def monitor(self, *args, timeout=30):
        return subprocess.run([sys.executable, 'monitor.py', *args, '--runtime-dir', self.runtime],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)

    def dashboard_pty(self, keys=(b'q',), extra=(), timeout=25):
        """Run the dashboard on a real terminal and return (exit code, rendered bytes)."""
        import fcntl
        import pty
        import select
        import struct
        import termios

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        process = subprocess.Popen([sys.executable, 'dashboard.py', *extra, '--runtime-dir', self.runtime],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, 'TERM': 'xterm-256color'}, start_new_session=True)
        os.close(slave)
        output = b''
        try:
            deadline = time.monotonic() + timeout
            while b'Demo link' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.3)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
            for key in keys:
                os.write(master, key)
                time.sleep(0.6)
                while select.select([master], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
            code = process.wait(timeout=timeout)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        return code, output

    def test_real_terminal_renders_and_quit_keeps_monitor_running(self):
        self.addCleanup(self.stop_monitor)
        code, output = self.dashboard_pty(extra=('--demo',))
        self.assertEqual(code, 0)
        self.assertIn(b'Demo link', output)
        self.assertIn(b'Stop monitor', output)
        # Quitting the dashboard is a view action: the detached monitor keeps running.
        status = subprocess.run([sys.executable, 'monitor.py', '--demo', '--status', '--runtime-dir', self.runtime],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('pid', status.stdout)

    def test_attach_reports_monitor_identity_and_stop_binding_stops_it(self):
        self.addCleanup(self.stop_monitor)
        self.assertEqual(self.monitor('--demo', '--interval', '3').returncode, 0)
        self.assertEqual(self.monitor('--demo', '--status').returncode, 0)
        # No --demo on this command line: badge and data must come from the monitor itself.
        code, output = self.dashboard_pty(keys=(b's', b'q'))
        self.assertEqual(code, 0)
        self.assertIn(b'Demo link', output)
        self.assertIn(b'DEMO', output)
        self.assertIn(b'stopped', output.lower())
        self.assertEqual(self.monitor('--demo', '--status').returncode, 2)


if __name__ == '__main__':
    unittest.main()
