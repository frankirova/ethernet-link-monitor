"""Credential loading through isolated CLI copies; all values are synthetic."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


class CredentialCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('monitor.py', 'monitor_runtime.py', 'monitor_credentials.py'):
            source = Path(name)
            if source.exists():
                shutil.copy(source, self.root / name)
        self.env = {k: v for k, v in os.environ.items() if k not in ('SNMP_COMMUNITY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(self.root / 'monitor.py'), *args], cwd=self.tmp.name,
                              env=self.env, capture_output=True, text=True, timeout=15)

    def credentials(self, text, mode=0o600):
        path = self.root / '.env'
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_malformed_file_is_rejected_before_discovery(self):
        self.credentials('SNMP_COMMUNITY=synthetic-secret\nBROKEN="unterminated\n')
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('credential file', result.stderr)
        self.assertNotIn('synthetic-secret', result.stderr)

    def test_unsafe_file_is_rejected(self):
        self.credentials('SNMP_COMMUNITY=synthetic-secret\n', 0o644)
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('credential file', result.stderr)
        self.assertNotIn('synthetic-secret', result.stderr)

    def test_fifo_file_is_rejected_without_blocking(self):
        os.mkfifo(self.root / '.env', 0o600)
        started = time.monotonic()
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('credential file', result.stderr)
        self.assertLess(time.monotonic() - started, 5)

    def test_demo_and_help_ignore_invalid_file(self):
        self.credentials('broken')
        for args in (('--demo', '--once'), ('--help',)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)

    def fake_network(self):
        path = self.root / 'monitor.py'
        source = path.read_text()
        fake = '''\nclass Snmp:
    def __init__(self): pass
    def close(self): pass
    async def discover(self, ip, community):
        record('discover', community)
        return ['2\\teth0']
    async def sample(self, host):
        record('sample', os.environ[host.community_env])
        return ('OK', 1000, '')

def record(kind, value):
    with open(os.environ['TEST_RECORD'], 'a') as stream:
        stream.write(json.dumps([kind, value, os.environ.get('TELEGRAM_BOT_TOKEN'),
                                 os.environ.get('TELEGRAM_CHAT_ID'), os.environ.get('LD_PRELOAD')]) + '\\n')
'''
        path.write_text(source.replace('def main():', fake + '\ndef main():'))
        self.env['TEST_RECORD'] = str(self.root / 'record')

    def hosts(self):
        path = self.root / 'hosts.csv'
        path.write_text('nombre,ip,modelo,zona,ifindex,min_mbps,community_env\nsynthetic,127.0.0.1,x,x,2,1000,CUSTOM_COMMUNITY\n')
        return str(path)

    def records(self):
        return [json.loads(line) for line in (self.root / 'record').read_text().splitlines()]

    def test_discovery_autoload_and_environment_precedence(self):
        self.fake_network()
        self.credentials('SNMP_COMMUNITY=file-secret\n')
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '2\teth0\n', ''))
        self.assertEqual(self.records()[0][1], 'file-secret')
        self.env['SNMP_COMMUNITY'] = 'shell-secret'
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '2\teth0\n', ''))
        self.assertEqual(self.records()[1][1], 'shell-secret')

    def test_once_literals_allowlist_and_owner_inheritance(self):
        self.fake_network()
        hosts = self.hosts()
        self.credentials('''# comment
CUSTOM_COMMUNITY='literal $(not-executed) # value'
TELEGRAM_BOT_TOKEN="token $HOME"
TELEGRAM_CHAT_ID=chat#literal
LD_PRELOAD=not-a-library
''')
        args = ('--hosts', hosts)
        result = self.cli(*args, '--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('literal $(not-executed)', result.stdout + result.stderr)
        expected = ['sample', 'literal $(not-executed) # value', 'token $HOME', 'chat#literal', None]
        self.assertEqual(self.records(), [expected])
        runtime = str(self.root / 'runtime')
        try:
            result = self.cli(*args, '--runtime-dir', runtime)
            self.assertEqual(result.returncode, 0, result.stderr)
            import time
            deadline = time.monotonic() + 5
            while len(self.records()) < 2 and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(self.records()[1], expected)
            self.credentials('invalid')
            attached = self.cli(*args, '--runtime-dir', runtime)
            self.assertEqual(attached.returncode, 0, attached.stderr)
            status = self.cli(*args, '--runtime-dir', runtime, '--status')
            self.assertEqual(status.returncode, 0, status.stderr)
            for text in (result.stdout, result.stderr, attached.stdout, attached.stderr, status.stdout, status.stderr,
                         (Path(runtime) / 'monitor.log').read_text()):
                self.assertNotIn('literal $(not-executed)', text)
                self.assertNotIn('token $HOME', text)
        finally:
            stopped = self.cli(*args, '--runtime-dir', runtime, '--stop')
            self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_invalid_inputs_never_mutate_environment_or_fixture(self):
        # The helper uses a subprocess so an attempted partial update is observable.
        helper = '''import json, os, monitor_credentials
try:
 monitor_credentials.load({'SNMP_COMMUNITY'})
except ValueError:
 pass
print(json.dumps(os.environ.get('SNMP_COMMUNITY')))
'''
        for content in ('SNMP_COMMUNITY=secret\nBAD="unterminated\n', 'SNMP_COMMUNITY=secret\n' + 'x' * 65537):
            with self.subTest(length=len(content)):
                path = self.credentials(content)
                before = path.read_bytes()
                result = subprocess.run([sys.executable, '-c', helper], cwd=self.root, env=self.env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'null\n', ''))
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.root / 'record').exists())

    def test_symlink_directory_and_oversize_rejected(self):
        path = self.root / '.env'
        target = self.root / 'fixture'
        target.write_text('SNMP_COMMUNITY=secret\n')
        target.chmod(0o600)
        path.symlink_to(target)
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('credential file', result.stderr)
        path.unlink()
        path.mkdir()
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('credential file', result.stderr)

    def test_import_and_absent_control_ignore_bad_file(self):
        self.credentials('broken')
        inventory = self.root / 'inventory.csv'
        inventory.write_text('nombre,ip\nPTP test,127.0.0.1\n')
        result = self.cli('--import-csv', str(inventory), '--output', str(self.root / 'out.csv'))
        self.assertEqual(result.returncode, 0, result.stderr)
        for action in ('--status', '--stop'):
            result = self.cli('--runtime-dir', str(self.root / 'runtime'), action)
            self.assertEqual(result.returncode, 2)
            self.assertIn('not running', result.stderr)
            self.assertNotIn('credential file', result.stderr)

    def test_missing_file_preserves_discovery_error(self):
        result = self.cli('--discover', '127.0.0.1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('SNMP_COMMUNITY', result.stderr)


if __name__ == '__main__':
    unittest.main()
