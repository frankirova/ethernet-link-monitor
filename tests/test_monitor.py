import asyncio
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import monitor
import monitor_runtime as runtime


class ConfigTests(unittest.TestCase):
    def test_import_ptp_only_and_manual_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'inventory.csv'
            source.write_text('nombre,ip,modelo,zona\nPTP north,192.0.2.1,AF60,North\nother,192.0.2.2,M5,South\n', encoding='utf-8')
            output = Path(tmp) / 'hosts.csv'
            command = [sys.executable, 'monitor.py', '--import-csv', str(source), '--output', str(output)]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual((first.returncode, first.stdout, first.stderr), (0, 'Imported 1 PTP hosts. Configure ifindex and min_mbps before monitoring.\n', ''))
            original = output.read_bytes()
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(second.stdout, '')
            self.assertIn('already exists', second.stderr)
            self.assertEqual(output.read_bytes(), original)
            with output.open('a') as stream:
                stream.write('Manual,192.0.2.3,AF60,South,2,100,SNMP_COMMUNITY\n')
            rows = monitor.load_hosts(output, demo=True)
            self.assertEqual([h.name for h in rows], ['PTP north', 'Manual'])
            with self.assertRaisesRegex(ValueError, 'ifindex'):
                monitor.load_hosts(output)

    def test_invalid_threshold_duplicate_and_ip_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'hosts.csv'
            header = 'nombre,ip,modelo,zona,ifindex,min_mbps,community_env\n'
            for row in ['x,192.0.2.1,,,1,0,SNMP_COMMUNITY\n', 'x,bad,,,1,100,SNMP_COMMUNITY\n', 'x,192.0.2.1,,,1,100,SNMP_COMMUNITY\nx,192.0.2.1,,,1,100,SNMP_COMMUNITY\n']:
                p.write_text(header + row)
                before = p.read_bytes()
                with self.assertRaises(ValueError):
                    monitor.load_hosts(p)
                self.assertEqual(p.read_bytes(), before)


class MonitorTests(unittest.TestCase):
    def test_minimum_boundaries_down_unknown_and_unreachable(self):
        for actual, minimum, expected in [(1000, 1000, 'OK'), (100, 1000, 'LOW'), (100, 100, 'OK'), (1000, 100, 'OK'), (10, 100, 'LOW')]:
            self.assertEqual(monitor.classify(1, actual, minimum), expected)
        self.assertEqual(monitor.classify(2, 0, 1000), 'DOWN')
        self.assertEqual(monitor.classify(1, None, 100), 'UNKNOWN')
        self.assertEqual(monitor.classify(4, 1000, 100), 'UNKNOWN')
        self.assertEqual(monitor.classify(1, 0, 100), 'UNKNOWN')

    def test_confirmation_no_repeats_and_recovery(self):
        state = monitor.Alarm()
        self.assertIsNone(state.observe('LOW'))
        self.assertEqual(state.observe('LOW'), 'LOW')
        state.delivered('LOW')
        self.assertIsNone(state.observe('LOW'))
        self.assertIsNone(state.observe('OK'))
        self.assertEqual(state.observe('OK'), 'OK')
        state.delivered('OK')
        self.assertIsNone(state.observe('OK'))

    def test_failed_delivery_retries_and_transient_is_silent(self):
        state = monitor.Alarm()
        self.assertIsNone(state.observe('UNREACHABLE'))
        self.assertIsNone(state.observe('OK'))
        self.assertIsNone(state.observe('DOWN'))
        self.assertEqual(state.observe('DOWN'), 'DOWN')
        self.assertEqual(state.observe('DOWN'), 'DOWN')
        self.assertIsNone(state.observe('OK'))
        self.assertIsNone(state.observe('OK'))  # No recovery for undelivered alarm.

    def test_each_failure_is_distinct_and_confirmed(self):
        alarm = monitor.Alarm()
        for status in ['UNREACHABLE', 'UNKNOWN', 'DOWN', 'LOW', 'OK']:
            self.assertIsNone(alarm.observe(status))
            self.assertEqual(alarm.observe(status), status)
            alarm.delivered(status)
            self.assertIsNone(alarm.observe(status))

    def test_telegram_failure_is_redacted_and_success_checked(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret-token', 'TELEGRAM_CHAT_ID': 'test-chat'}):
            with patch('urllib.request.OpenerDirector.open', side_effect=OSError('secret-token')):
                with self.assertRaisesRegex(RuntimeError, '^Telegram delivery failed; will retry next scan$'):
                    monitor.send_telegram('test')
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"ok":true}'
            with patch('urllib.request.OpenerDirector.open', return_value=response) as send:
                monitor.send_telegram('test')
                request = send.call_args.args[0]
                self.assertEqual(json.loads(request.data), {'chat_id': 'test-chat', 'text': 'test'})


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_pipeline_sends_alarm_once_and_recovery(self):
        from argparse import Namespace
        args = Namespace(demo=False, once=False, interval=0)
        host = monitor.Host('test', '192.0.2.1', 2, 1000)
        observations = iter(['LOW', 'LOW', 'LOW', 'OK', 'OK'])
        class FakeSnmp:
            calls = 0
            async def sample(self, host):
                self.calls += 1
                if self.calls == 5:
                    args.once = True
                status = next(observations)
                return status, 100 if status == 'LOW' else 1000, ''
            def close(self):
                pass
        with patch('monitor.Snmp', FakeSnmp), patch('monitor.send_telegram') as send, contextlib.redirect_stdout(io.StringIO()):
            result = await monitor.run(args, [host])
        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 2)
        self.assertIn('ALERT: test (192.0.2.1) Ethernet #2: LOW, 100 Mbps; minimum 1000 Mbps', send.call_args_list[0].args[0])
        self.assertIn('RECOVERY: test', send.call_args_list[1].args[0])


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, 'monitor.py', *args], text=True, capture_output=True)

    def test_demo_no_credentials_or_network(self):
        result = self.run_cli('--demo', '--once', '--headless')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertIn('DEMO', result.stdout)
        for status in ['OK', 'LOW', 'DOWN', 'UNREACHABLE', 'UNKNOWN']:
            self.assertIn(status, result.stdout)

    def test_demo_cannot_be_combined_with_network_or_import_modes(self):
        for extra in [['--discover', '127.0.0.1'], ['--import-csv', 'absent.csv']]:
            result = self.run_cli('--demo', *extra)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('not allowed with argument --demo', result.stderr)
        with patch.object(sys, 'argv', ['monitor.py', '--demo', '--discover', '127.0.0.1']), patch('monitor.Snmp') as client, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                monitor.main()
            self.assertEqual(error.exception.code, 2)
            client.assert_not_called()

    def test_owner_mode_publishes_the_snapshot_viewers_read(self):
        """A supervisor runs this mode, so it must be the one viewers and --status can see."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / 'rt'
            process = subprocess.Popen(
                [sys.executable, 'monitor.py', '--demo', '--owner', '--runtime-dir', str(runtime_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                snapshot = None
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    snapshot = runtime.request(runtime_dir, 'status') if runtime_dir.exists() else None
                    if snapshot and snapshot.get('records'):
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(snapshot, 'owner mode never published a snapshot')
                self.assertTrue(snapshot['records'], 'the published snapshot must carry per-link rows')
                self.assertEqual(sorted(snapshot['records'][0]),
                                 ['detail', 'index', 'ip', 'minimum', 'name', 'speed', 'status'])
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()

    def test_output_requires_import_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'chosen.csv'
            output.write_bytes(b'unchanged')
            result = self.run_cli('--demo', '--once', '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('--output requires --import-csv', result.stderr)
            self.assertEqual(output.read_bytes(), b'unchanged')

    def test_help_and_invalid_options(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0)
        for flag in ['--demo', '--headless', '--discover', '--import-csv', '--once']:
            self.assertIn(flag, result.stdout)
        result = self.run_cli('--interval', '0', '--demo', '--once')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('interval must be positive', result.stderr)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'PTY test requires Linux')
    def test_tui_renders_and_quits_in_real_terminal(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        tmp = tempfile.TemporaryDirectory()
        runtime = str(Path(tmp.name) / 'runtime')
        self.addCleanup(tmp.cleanup)
        self.addCleanup(self.run_cli, '--demo', '--runtime-dir', runtime, '--stop')
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        env = {**os.environ, 'TERM': 'xterm-256color'}
        process = subprocess.Popen([sys.executable, 'monitor.py', '--demo', '--interval', '0.1', '--runtime-dir', runtime], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        output = b''
        try:
            deadline = time.monotonic() + 5
            while b'ETHERNET MONITOR' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b'ETHERNET MONITOR', output)
            self.assertIn(b'DEMO', output)
            os.write(master, b'jq')
            self.assertEqual(process.wait(timeout=3), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'PTY test requires Linux')
    def test_closing_viewer_leaves_demo_monitor_running(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        import time
        with tempfile.TemporaryDirectory() as tmp:
            runtime = str(Path(tmp) / 'runtime')
            common = ['--demo', '--interval', '0.1', '--runtime-dir', runtime]
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
            env = {**os.environ, 'TERM': 'xterm-256color'}
            process = subprocess.Popen([sys.executable, 'monitor.py', *common],
                                       stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            output = b''
            try:
                deadline = time.monotonic() + 5
                while b'ETHERNET MONITOR' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.2)[0]:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                self.assertIn(b'ETHERNET MONITOR', output)
                os.write(master, b'q')
                self.assertEqual(process.wait(timeout=3), 0)
                status = self.run_cli('--demo', '--runtime-dir', runtime, '--status')
                self.assertEqual(status.returncode, 0, status.stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                self.run_cli('--demo', '--runtime-dir', runtime, '--stop')

    def test_real_missing_config_is_actionable(self):
        result = self.run_cli('--hosts', 'does-not-exist.csv', '--once', '--headless')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
