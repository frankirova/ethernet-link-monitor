import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import monitor_runtime


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = str(Path(self.tmp.name) / 'runtime')
        self.common = ['--demo', '--runtime-dir', self.runtime]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.cli, '--demo', '--runtime-dir', self.runtime, '--stop')

    def cli(self, *args):
        return subprocess.run([sys.executable, 'monitor.py', *args], capture_output=True, text=True, timeout=15)

    def status(self):
        result = self.cli(*self.common, '--status')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def start(self):
        result = self.cli(*self.common, '--interval', '0.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.status()

    def snapshot(self):
        return monitor_runtime.request(Path(self.runtime), 'status')

    def test_snapshot_publishes_host_records_and_notices(self):
        self.start()
        deadline = time.monotonic() + 5
        snapshot = self.snapshot()
        while snapshot and {record['status'] for record in snapshot.get('records') or ()} == {'PENDING'} and time.monotonic() < deadline:
            time.sleep(0.1)
            snapshot = self.snapshot()
        self.assertIsNotNone(snapshot)
        self.assertTrue({'pid', 'scans', 'updated', 'identity', 'lines', 'records', 'notices'} <= set(snapshot))
        records = snapshot['records']
        self.assertEqual(len(records), 5)
        for index, record in enumerate(records, start=1):
            self.assertEqual(record['name'], f'Demo link {index}')
            self.assertEqual(record['ip'], f'192.0.2.{index}')
            self.assertEqual(record['index'], 1)
            self.assertEqual(record['minimum'], 1000)
            self.assertIn(record['status'], {'OK', 'LOW', 'DOWN', 'UNREACHABLE', 'UNKNOWN'})
            self.assertIsInstance(record['detail'], str)
        self.assertIsInstance(snapshot['notices'], list)
        # The rendered lines stay available for the existing viewer.
        self.assertIn('ETHERNET MONITOR', '\n'.join(snapshot['lines']))

    def test_start_reattach_progress_conflict_and_stop(self):
        first = self.start()
        deadline = time.monotonic() + 4
        current = first
        while current['scans'] <= first['scans'] and time.monotonic() < deadline:
            time.sleep(0.1)
            current = self.status()
        self.assertGreater(current['scans'], first['scans'])
        self.assertEqual(self.start()['pid'], first['pid'])
        conflict = self.cli(*self.common, '--interval', '0.2')
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('conflicts', conflict.stderr)
        live = self.cli('--runtime-dir', self.runtime, '--status')
        self.assertEqual(live.returncode, 2)
        self.assertIn('conflicts', live.stderr)
        stopped = self.cli(*self.common, '--stop')
        self.assertEqual((stopped.returncode, stopped.stdout, stopped.stderr), (0, 'Monitor stopped.\n', ''))
        self.assertEqual(self.cli(*self.common, '--status').returncode, 2)

    def test_concurrent_starts_share_owner(self):
        command = [sys.executable, 'monitor.py', *self.common, '--interval', '0.1']
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, (out, err))
        self.assertEqual(self.start()['pid'], self.status()['pid'])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'requires PTY')
    def test_terminal_disappearance_keeps_owner(self):
        import pty
        import select
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, 'monitor.py', *self.common, '--interval', '0.1'],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, 'TERM': 'xterm-256color'}, start_new_session=True)
        os.close(slave)
        try:
            deadline = time.monotonic() + 5
            output = b''
            while b'ETHERNET MONITOR' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b'ETHERNET MONITOR', output)
            before = self.status()
            os.close(master)
            master = None
            process.wait(timeout=5)
            time.sleep(0.4)
            after = self.status()
            self.assertEqual(before['pid'], after['pid'])
            self.assertGreater(after['scans'], before['scans'])
        finally:
            if master is not None:
                os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_absent_invalid_and_private_runtime(self):
        for action in ('--status', '--stop'):
            result = self.cli(*self.common, action)
            self.assertEqual(result.returncode, 2)
            self.assertIn('not running', result.stderr)
        result = self.cli('--hosts', str(Path(self.tmp.name) / 'missing'), '--runtime-dir', self.runtime)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.cli(*self.common, '--status').returncode, 2)
        os.chmod(self.runtime, 0o755)
        result = self.cli(*self.common, '--status')
        self.assertEqual(result.returncode, 2)
        self.assertIn('owner-only', result.stderr)
        os.chmod(self.runtime, 0o700)

    def test_umask_new_runtime_parents_are_private(self):
        parent = Path(self.tmp.name) / 'new-parent'
        runtime = parent / 'nested' / 'runtime'
        result = subprocess.run(
            [sys.executable, '-c', 'import monitor_runtime, sys; monitor_runtime.directory(sys.argv[1])', str(runtime)],
            capture_output=True, text=True, timeout=10, umask=0o002)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (parent, parent / 'nested', runtime):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, str(path))

    def test_umask_cli_start_progress_attach_and_stop(self):
        self.runtime = str(Path(self.tmp.name) / 'new-parent' / 'runtime')
        self.common = ['--demo', '--runtime-dir', self.runtime]
        # The original cleanup was registered before this test selected its path.
        self.addCleanup(self.cli, *self.common, '--stop')
        env = dict(os.environ)
        env.pop('XDG_RUNTIME_DIR', None)
        command = [sys.executable, 'monitor.py', *self.common, '--interval', '0.1']
        started = subprocess.run(command, capture_output=True, text=True, timeout=15, env=env, umask=0o002)
        self.assertEqual(started.returncode, 0, started.stderr)
        first = self.status()
        attached = subprocess.run(command, capture_output=True, text=True, timeout=15, env=env, umask=0o002)
        self.assertEqual(attached.returncode, 0, attached.stderr)
        deadline = time.monotonic() + 4
        current = self.status()
        while current['scans'] <= first['scans'] and time.monotonic() < deadline:
            time.sleep(0.1)
            current = self.status()
        self.assertEqual(current['pid'], first['pid'])
        self.assertGreater(current['scans'], first['scans'])
        stopped = self.cli(*self.common, '--stop')
        self.assertEqual((stopped.returncode, stopped.stdout, stopped.stderr), (0, 'Monitor stopped.\n', ''))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(first['pid'], 0)
            except ProcessLookupError:
                break
            # A zombie has terminated and cannot continue polling, even if init has not reaped it.
            proc = Path(f'/proc/{first["pid"]}/stat')
            if proc.exists() and proc.read_text().split(') ', 1)[1].startswith('Z '):
                break
            time.sleep(0.05)
        else:
            self.fail('monitor process still alive after stop')

    def test_umask_existing_writable_parent_is_rejected_unchanged(self):
        parent = Path(self.tmp.name) / 'unsafe'
        parent.mkdir(mode=0o700)
        parent.chmod(0o775)
        result = self.cli('--demo', '--runtime-dir', str(parent / 'runtime'), '--status')
        self.assertEqual(result.returncode, 2)
        self.assertIn('unsafe writable parent', result.stderr)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o775)
        self.assertFalse((parent / 'runtime').exists())

    def test_headless_remains_foreground_and_terminates(self):
        process = subprocess.Popen([sys.executable, 'monitor.py', '--demo', '--headless', '--interval', '0.1'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn('ETHERNET MONITOR', process.stdout.readline())
            process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(self.cli(*self.common, '--status').returncode, 2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
