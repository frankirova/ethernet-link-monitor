import http.client
import json
from pathlib import Path
import threading
import time
import unittest

import monitor_view
import web

WEB_ROOT = Path(web.__file__).with_name('web')


def record(name='Link', ip='192.0.2.1', index=1, speed=1000, minimum=1000, status='OK', detail=''):
    return {'name': name, 'ip': ip, 'index': index, 'speed': speed, 'minimum': minimum,
            'status': status, 'detail': detail}


def snapshot(records, updated=None, interval=60, demo=False, hosts='/home/operator/interfaces-alert/hosts.csv'):
    return {'pid': 4242, 'identity': {'hosts': hosts, 'demo': demo, 'interval': interval},
            'scans': 7, 'updated': time.time() if updated is None else updated,
            'records': records, 'notices': ['ALERT: Demo link 3 DOWN'], 'lines': ['ETHERNET MONITOR | LIVE']}


class FakeClient:
    """Stands in for the IPC client so the server is tested without a monitor."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.path = Path('/tmp/runtime')
        self.started = False

    def attach(self):
        return self.snapshots[0]

    def snapshot(self):
        return self.snapshots[-1]


class PayloadTests(unittest.TestCase):
    def test_links_are_worst_first_and_classified(self):
        view = monitor_view.describe(snapshot([
            record('Alpha', '192.0.2.1', status='OK'),
            record('Beta', '192.0.2.2', status='DOWN', speed=None, detail='Ethernet is not operational'),
            record('Gamma', '192.0.2.3', status='LOW', speed=100, minimum=1000),
            record('Delta', '192.0.2.4', status='UNREACHABLE', speed=None),
        ]), interval=60)
        payload = monitor_view.snapshot_payload(view)
        self.assertEqual([link['name'] for link in payload['links']], ['Beta', 'Gamma', 'Delta', 'Alpha'])
        self.assertEqual([link['tone'] for link in payload['links']], ['alarm', 'alarm', 'nodata', 'ok'])
        self.assertEqual(payload['verdict'], {'total': 4, 'out_of_spec': 2, 'no_data': 1, 'ok': 1})
        self.assertEqual(payload['counts'], {'OK': 1, 'DOWN': 1, 'LOW': 1, 'UNREACHABLE': 1})
        self.assertEqual(payload['links'][0]['speed_text'], '-')
        self.assertEqual(payload['links'][1]['speed_text'], '100M')
        self.assertEqual(payload['links'][1]['minimum_text'], '1.0G')
        self.assertEqual(payload['state'], 'live')

    def test_payload_never_exposes_the_absolute_hosts_path(self):
        payload = monitor_view.snapshot_payload(
            monitor_view.describe(snapshot([record()], hosts='/home/operator/private/hosts.csv')))
        self.assertEqual(payload['source'], 'hosts.csv')
        self.assertNotIn('operator', json.dumps(payload))

    def test_status_the_views_do_not_know_is_never_reported_as_healthy(self):
        payload = monitor_view.snapshot_payload(
            monitor_view.describe(snapshot([record('Weird', status='FLAPPING')])))
        self.assertEqual(payload['links'][0]['tone'], 'nodata')
        self.assertEqual(payload['verdict'], {'total': 1, 'out_of_spec': 0, 'no_data': 1, 'ok': 0})

    def test_disconnected_payload_is_empty_and_honest(self):
        payload = monitor_view.snapshot_payload(monitor_view.describe(None, interval=60))
        self.assertEqual(payload['state'], 'disconnected')
        self.assertEqual(payload['links'], [])
        self.assertEqual(payload['verdict'], {'total': 0, 'out_of_spec': 0, 'no_data': 0, 'ok': 0})
        self.assertEqual(payload['source'], '')
        self.assertFalse(payload['demo'])

    def test_stale_snapshot_keeps_its_age_and_state(self):
        view = monitor_view.describe(snapshot([record()], updated=time.time() - 900, interval=3), interval=3)
        payload = monitor_view.snapshot_payload(view)
        self.assertEqual(payload['state'], 'stale')
        self.assertGreater(payload['age'], 800)


class ServerTests(unittest.TestCase):
    def serve(self, snapshots):
        server = web.make_server(0, FakeClient(snapshots), interval=60)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def get(self, port, path, method='GET'):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_snapshot_endpoint_answers_with_the_payload(self):
        port = self.serve([snapshot([record('Beta', status='DOWN', speed=None)])])
        status, headers, body = self.get(port, '/api/snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        payload = json.loads(body)
        self.assertEqual(payload['links'][0]['status'], 'DOWN')
        self.assertIn('verdict', payload)

    def test_index_is_served_with_a_strict_policy(self):
        port = self.serve([snapshot([record()])])
        status, headers, body = self.get(port, '/')
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        policy = headers['Content-Security-Policy']
        self.assertIn("default-src 'none'", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn('https:', policy)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn(b'enlaces', body)

    def test_static_assets_and_unknown_paths(self):
        port = self.serve([snapshot([record()])])
        for path, kind in (('/app.js', 'text/javascript'), ('/styles.css', 'text/css'),
                           ('/favicon.svg', 'image/svg+xml')):
            status, headers, body = self.get(port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers['Content-Type'].startswith(kind), path)
            self.assertTrue(body, path)
        for path in ('/nope', '/../monitor.py', '/web/app.js', '/%2e%2e/monitor.py'):
            status, _, _ = self.get(port, path)
            self.assertEqual(status, 404, path)

    def test_every_other_verb_is_refused_with_allow_and_policy(self):
        port = self.serve([snapshot([record()])])
        verbs = ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'TRACE', 'CONNECT', 'FROBNICATE')
        for method in verbs:
            for path in ('/', '/api/snapshot'):
                status, headers, body = self.get(port, path, method)
                self.assertEqual(status, 405, f'{method} {path}')
                self.assertEqual(headers['Allow'], 'GET', f'{method} {path}')
                # The stdlib fallback answers 501 without these; every refusal must carry them.
                self.assertEqual(headers['Cache-Control'], 'no-store', f'{method} {path}')
                self.assertIn("default-src 'none'", headers['Content-Security-Policy'], f'{method} {path}')
                self.assertEqual(body, b'', f'{method} {path}')

    def test_server_binds_loopback_and_refuses_a_busy_port(self):
        server = web.make_server(0, FakeClient([snapshot([record()])]))
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        with self.assertRaises(OSError):
            web.make_server(server.server_address[1], FakeClient([None])).server_close()

    def test_an_abbreviated_option_is_refused(self):
        # --host is not an option here; it must not silently become --hosts.
        with self.assertRaises(SystemExit):
            web.parse_args(['--host', '0.0.0.0'])

    def test_a_disconnected_monitor_still_serves_the_page(self):
        port = self.serve([None])
        status, _, body = self.get(port, '/api/snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['state'], 'disconnected')


class StaticTests(unittest.TestCase):
    def test_assets_are_offline_and_self_contained(self):
        assets = ['index.html', 'styles.css', 'app.js', 'favicon.svg']
        for name in assets:
            path = WEB_ROOT / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding='utf-8')
            # The SVG namespace is the only URL a self-contained page may name.
            text = text.replace('http://www.w3.org/2000/svg', '')
            for marker in ('http://', 'https://', '//cdn'):
                self.assertNotIn(marker, text, f'{name} must not reach the network')
        index = (WEB_ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertIn('styles.css', index)
        self.assertIn('app.js', index)
        self.assertIn('api/snapshot', (WEB_ROOT / 'app.js').read_text(encoding='utf-8'))


class ArgumentTests(unittest.TestCase):
    def test_interval_must_be_positive_and_finite(self):
        with self.assertRaises(SystemExit):
            web.parse_args(['--interval', '0'])
        self.assertEqual(web.parse_args([]).interval, 60)

    def test_port_bounds(self):
        self.assertEqual(web.parse_args([]).port, web.DEFAULT_PORT)
        self.assertEqual(web.parse_args(['--port', '0']).port, 0)
        for value in ('70000', '-1', 'http'):
            with self.assertRaises(SystemExit, msg=value):
                web.parse_args(['--port', value])


if __name__ == '__main__':
    unittest.main()
