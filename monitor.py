#!/usr/bin/env python3
"""Small Ethernet monitor with environment and private file credentials."""
import argparse
import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request

import monitor_runtime as runtime
import monitor_credentials

FIELDS = ['nombre', 'ip', 'modelo', 'zona', 'ifindex', 'min_mbps', 'community_env']
IF_TABLE = '1.3.6.1.2.1.2.2.1'
HIGH_SPEED = '1.3.6.1.2.1.31.1.1.1.15'


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


@dataclass(frozen=True)
class Host:
    name: str
    ip: str
    index: int
    minimum: int
    community_env: str = 'SNMP_COMMUNITY'


def load_hosts(path, demo=False):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not set(FIELDS).issubset(reader.fieldnames or []):
            raise ValueError('hosts CSV must contain: ' + ','.join(FIELDS))
        hosts, seen = [], set()
        for line, row in enumerate(reader, 2):
            try:
                ip = str(ipaddress.IPv4Address(row['ip'].strip()))
                index = int(row['ifindex'] or (1 if demo else ''))
                minimum = int(row['min_mbps'] or (1000 if demo else ''))
                env = row['community_env'].strip() or 'SNMP_COMMUNITY'
                name = clean(row['nombre']).strip()
                if not name or index < 1 or minimum not in (100, 1000):
                    raise ValueError()
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env):
                    raise ValueError()
                if (ip, index) in seen:
                    raise ValueError()
            except (ValueError, AttributeError, TypeError):
                raise ValueError(f'hosts CSV line {line}: require valid IPv4, name, unique ip/ifindex, positive ifindex, min_mbps 100 or 1000 and environment variable name') from None
            seen.add((ip, index))
            hosts.append(Host(name, ip, index, minimum, env))
    if not hosts:
        raise ValueError('hosts CSV is empty')
    return hosts


def import_inventory(source, destination):
    with Path(source).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'nombre', 'ip'}.issubset(reader.fieldnames or []):
            raise ValueError('inventory CSV must contain nombre and ip')
        rows = []
        seen = set()
        for row in reader:
            if 'ptp' not in (row.get('nombre') or '').lower():
                continue
            ip = str(ipaddress.IPv4Address(row['ip'].strip()))
            if ip in seen:
                raise ValueError('inventory contains duplicate PTP IPs')
            seen.add(ip)
            rows.append({**{key: row.get(key, '') for key in FIELDS},
                         'ifindex': '', 'min_mbps': '', 'community_env': 'SNMP_COMMUNITY'})
    if not rows:
        raise ValueError('inventory contains no PTP hosts')
    try:
        with Path(destination).open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    except FileExistsError:
        raise ValueError('output already exists; choose another --output file') from None
    return len(rows)


def classify(oper_status, speed, minimum):
    if oper_status in (2, 6, 7):
        return 'DOWN'
    if oper_status != 1 or speed is None or speed <= 0:
        return 'UNKNOWN'
    return 'LOW' if speed < minimum else 'OK'


class Alarm:
    """Confirm observations; acknowledge only successful Telegram deliveries."""
    def __init__(self):
        self.candidate = None
        self.count = 0
        self.confirmed = 'OK'
        self.announced = 'OK'

    def observe(self, status):
        self.count = self.count + 1 if status == self.candidate else 1
        self.candidate = status
        if self.count >= 2:
            self.confirmed = status
        if status == self.confirmed and self.confirmed != self.announced:
            return self.confirmed
        return None

    def delivered(self, status):
        self.announced = status


def send_telegram(message):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat = os.environ['TELEGRAM_CHAT_ID']
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=json.dumps({'chat_id': chat, 'text': message}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        # Do not follow redirects that could expose the bot token.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=10) as response:
            result = json.loads(response.read(65536))
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('rejected')
    except Exception:
        # HTTP errors often contain the URL, including the bot token.
        raise RuntimeError('Telegram delivery failed; will retry next scan') from None


class Snmp:
    def __init__(self):
        try:
            from pysnmp.hlapi.v3arch import asyncio as api
        except ImportError:
            raise ValueError('missing pysnmp: run uv run --with "pysnmp>=7.1,<8" monitor.py ...') from None
        self.api = api
        self.engine = api.SnmpEngine()
        self.versions = {}

    def close(self):
        self.engine.close_dispatcher()

    async def target(self, ip):
        return await self.api.UdpTransportTarget.create((ip, 161), timeout=2, retries=0)

    async def get(self, target, community, *oids, model=1):
        a = self.api
        indication, status, _, bindings = await a.get_cmd(
            self.engine, a.CommunityData(community, mpModel=model), target,
            a.ContextData(), *(a.ObjectType(a.ObjectIdentity(oid)) for oid in oids),
            lookupMib=False)
        if indication:
            from pysnmp.proto.errind import RequestTimedOut
            if isinstance(indication, RequestTimedOut):
                raise TimeoutError()
            raise ValueError('SNMP request failed')
        if status:
            raise ValueError('SNMP agent returned an error')
        return [value for _, value in bindings]

    async def model_for(self, target, community, ip):
        """SNMPv2c first, then SNMPv1 for devices that only answer v1."""
        model = self.versions.get(ip)
        if model is not None:
            return model
        for candidate in (1, 0):
            try:
                await self.get(target, community, '1.3.6.1.2.1.1.1.0', model=candidate)
            except TimeoutError:
                continue
            self.versions[ip] = candidate
            return candidate
        raise TimeoutError()

    async def sample(self, host):
        try:
            target = await self.target(host.ip)
            community = os.environ[host.community_env]
            model = await self.model_for(target, community, host.ip)
            kind, oper, speed = await self.get(target, community,
                f'{IF_TABLE}.3.{host.index}', f'{IF_TABLE}.8.{host.index}',
                f'{IF_TABLE}.5.{host.index}', model=model)
            if int(kind) != 6:
                return ('UNKNOWN', None, 'Configured interface is not Ethernet')
            oper = int(oper)
            if oper in (2, 6, 7):
                return ('DOWN', None, 'Ethernet is not operational')
            try:
                mbps = int(speed) / 1_000_000
            except (ValueError, TypeError):
                mbps = None
            if mbps is None or mbps < 1 or mbps >= 4294:
                high, = await self.get(target, community, f'{HIGH_SPEED}.{host.index}', model=model)
                mbps = int(high)
            return (classify(oper, mbps, host.minimum), mbps, '')
        except TimeoutError:
            self.versions.pop(host.ip, None)
            return ('UNREACHABLE', None, 'No SNMP response; port status is unknown')
        except Exception:
            return ('UNKNOWN', None, 'Check SNMP access, ifindex and supported OIDs')

    async def discover(self, ip, community):
        a = self.api
        target = await self.target(ip)
        model = await self.model_for(target, community, ip)
        interfaces = []
        async for error, status, _, bindings in a.bulk_walk_cmd(
                self.engine, a.CommunityData(community, mpModel=model), target,
                a.ContextData(), 0, 20,
                a.ObjectType(a.ObjectIdentity(f'{IF_TABLE}.3')),
                lexicographicMode=False, lookupMib=False, maxRows=256):
            if error or status:
                raise ValueError('SNMP discovery failed; check connectivity and community')
            for oid, value in bindings:
                if int(value) == 6:
                    index = int(oid[-1])
                    name, = await self.get(target, community, f'{IF_TABLE}.2.{index}', model=model)
                    interfaces.append(f'{index}\t{clean(name.prettyPrint())}')
        return interfaces


def demo_sample(position, cycle):
    states = ['OK', 'LOW', 'DOWN', 'UNREACHABLE', 'UNKNOWN']
    status = states[(position + cycle // 3) % len(states)]
    speed = 1000 if status == 'OK' else 100 if status == 'LOW' else None
    return status, speed, 'Synthetic data; no network or Telegram calls'


def table_lines(rows, demo, notices):
    title = 'ETHERNET MONITOR' + (' | DEMO — NO REAL ALERTS' if demo else ' | LIVE')
    lines = [title, 'Time: ' + datetime.now().strftime('%H:%M:%S'),
             'q: close viewer | arrows/j/k: scroll',
             'NAME                         IP              PORT  Mbps   MIN   STATUS', '-' * 86]
    for host, status, speed, detail in rows:
        value = '-' if speed is None else f'{speed:g}'
        lines.append(f'{host.name[:28]:28} {host.ip:15} {host.index:<5} {value:<6} {host.minimum:<5} {status}')
        if detail and status != 'OK':
            lines.append('  ' + detail)
    return lines + ['-' * 86] + notices[-3:]


def host_records(rows):
    return [{'name': host.name, 'ip': host.ip, 'index': host.index, 'speed': speed,
             'minimum': host.minimum, 'status': status, 'detail': detail}
            for host, status, speed, detail in rows]


async def run(args, hosts, screen=None, owner=None):
    client = None if args.demo else Snmp()
    alarms = [Alarm() for _ in hosts]
    rows = [(h, 'PENDING', None, 'Awaiting first sample') for h in hosts]
    notices = ['Demo only.' if args.demo else 'Telegram ready. Confirming transitions after 2 scans.']
    offset = cycle = 0

    async def scan():
        for i, host in enumerate(hosts):
            if args.demo:
                observation = demo_sample(i, cycle)
            else:
                try:
                    observation = await asyncio.wait_for(client.sample(host), timeout=8)
                except TimeoutError:
                    observation = ('UNREACHABLE', None, 'SNMP operation timed out')
            status, speed, detail = observation
            rows[i] = (host, status, speed, detail)
            change = alarms[i].observe(status)
            if change and not args.demo:
                value = '-' if speed is None else f'{speed:g}'
                message = f'{"RECOVERY" if change == "OK" else "ALERT"}: {host.name} ({host.ip}) Ethernet #{host.index}: {change}, {value} Mbps; minimum {host.minimum} Mbps'
                try:
                    await asyncio.to_thread(send_telegram, message)
                except RuntimeError as error:
                    notices.append(str(error))
                else:
                    alarms[i].delivered(change)
                    notices.append(message)
            del notices[:-3]

    task = None
    try:
        while True:
            task = asyncio.create_task(scan())
            while not task.done():
                if owner and owner.stopped.is_set():
                    return 0
                if screen:
                    draw(screen, table_lines(rows, args.demo, notices), offset)
                    offset = scroll(screen, offset, len(table_lines(rows, args.demo, notices)))
                await asyncio.sleep(0.05)
            await task
            if owner:
                owner.update(table_lines(rows, args.demo, notices), host_records(rows), notices[-3:])
            elif screen is None:
                print('\n'.join(table_lines(rows, args.demo, notices)), flush=True)
            if args.once:
                return 0 if args.demo or all(row[1] == 'OK' for row in rows) else 1
            cycle += 1
            deadline = time.monotonic() + args.interval
            while time.monotonic() < deadline:
                if owner and owner.stopped.is_set():
                    return 0
                if screen:
                    lines = table_lines(rows, args.demo, notices)
                    draw(screen, lines, offset)
                    offset = scroll(screen, offset, len(lines))
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if client:
            client.close()


def draw(screen, lines, offset):
    import curses
    screen.erase()
    height, width = screen.getmaxyx()
    fixed = lines[:5]
    body = lines[5 + offset:5 + offset + max(0, height - 6)]
    for y, line in enumerate((fixed + body)[:max(0, height - 1)]):
        try:
            screen.addnstr(y, 0, clean(line), max(0, width - 1))
        except curses.error:
            pass
    screen.refresh()


def scroll(screen, offset, total):
    import curses
    key = screen.getch()
    if key in (ord('q'), ord('Q')):
        raise KeyboardInterrupt()
    if key in (curses.KEY_DOWN, ord('j')):
        offset += 1
    elif key in (curses.KEY_UP, ord('k')):
        offset -= 1
    return max(0, min(offset, max(0, total - screen.getmaxyx()[0] + 1)))


def view(path, args):
    def lines():
        snapshot = runtime.request(path, 'status')
        if not snapshot:
            return ['MONITOR DISCONNECTED', 'Monitor is not running or cannot be reached.', 'q: close viewer']
        result = list(snapshot['lines'])
        age = time.time() - snapshot['updated'] if snapshot['updated'] else 0
        if snapshot['updated'] and age > max(30, args.interval + 20):
            return ['MONITOR STALE', 'No recent completed scan; status below may be out of date.', 'q: close viewer'] + result
        return result
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('\n'.join(lines()))
        return 0
    import curses
    def tui(screen):
        screen.nodelay(True)
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        offset = 0
        import select
        terminal = select.poll()
        terminal.register(sys.stdin.fileno(), select.POLLHUP | select.POLLERR)
        while True:
            if terminal.poll(0):
                return 0
            current = lines()
            draw(screen, current, offset)
            offset = scroll(screen, offset, len(current))
            time.sleep(0.1)
    return curses.wrapper(tui)


def main():
    parser = argparse.ArgumentParser(description='Monitor Ethernet minimum speed with SNMPv2c and Telegram. Default: terminal dashboard.', epilog='Real monitoring and discovery load credentials from the private .env beside monitor.py; existing environment variables take precedence.')
    parser.add_argument('--hosts', default='hosts.csv', help='editable host CSV (default: hosts.csv)')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo', action='store_true', help='synthetic data; no network, credentials or Telegram; exclusive with import/discovery')
    foreground = parser.add_mutually_exclusive_group()
    foreground.add_argument('--headless', action='store_true',
                            help='print scans without a TUI; publishes no snapshot, so viewers and --status cannot see it')
    foreground.add_argument('--owner', '--_owner', dest='_owner', action='store_true',
                            help='run in the foreground, publish the snapshot for viewers and --status, and hold the single-monitor lock; a supervisor owns its lifetime')
    parser.add_argument('--once', action='store_true', help='print one scan and exit; real unhealthy scan exits 1; no two-scan alerts')
    parser.add_argument('--interval', type=float, default=60, help='seconds between completed scans (default: 60)')
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument('--status', action='store_true', help='show background monitor status without starting it')
    controls.add_argument('--stop', action='store_true', help='explicitly stop the background monitor')
    parser.add_argument('--runtime-dir', default=os.path.join(os.environ.get('XDG_RUNTIME_DIR', f'/tmp/ethernet-monitor-{os.getuid()}'), 'ethernet-monitor'), help='private directory for local monitor IPC')
    modes.add_argument('--import-csv', metavar='FILE', help='import names containing PTP from inventory; no network; exclusive with demo/discovery')
    parser.add_argument('--output', help='import-only destination (default: hosts.csv); never overwrites')
    modes.add_argument('--discover', metavar='IP', help='list Ethernet ifindex/description for one IPv4 host; no Telegram; exclusive with demo/import')
    args = parser.parse_args()
    try:
        if args.output is not None and not args.import_csv:
            raise ValueError('--output requires --import-csv')
        if not 0 < args.interval < float('inf'):
            raise ValueError('interval must be positive and finite')
        if (args.status or args.stop) and (args.headless or args.once or args.import_csv or args.discover):
            raise ValueError('--status and --stop cannot be combined with foreground or import/discovery modes')
        if args.import_csv:
            count = import_inventory(args.import_csv, args.output or 'hosts.csv')
            print(f'Imported {count} PTP hosts. Configure ifindex and min_mbps before monitoring.')
            return 0
        if args.discover:
            ip = str(ipaddress.IPv4Address(args.discover))
            monitor_credentials.load({'SNMP_COMMUNITY'})
            community = os.environ.get('SNMP_COMMUNITY')
            if not community:
                raise ValueError('set SNMP_COMMUNITY for discovery')
            async def discover():
                client = Snmp()
                try:
                    return await asyncio.wait_for(client.discover(ip, community), timeout=20)
                finally:
                    client.close()
            for line in asyncio.run(discover()):
                print(line)
            return 0
        if args.status or args.stop:
            path = runtime.directory(args.runtime_dir)
            snapshot = runtime.request(path, 'status')
            if not snapshot:
                raise ValueError('monitor not running; start it first')
            if snapshot['identity']['demo'] != args.demo or snapshot['identity']['hosts'] != runtime.identity(args)['hosts']:
                raise ValueError('monitor configuration conflicts; select the matching --demo and --hosts')
            if args.stop:
                snapshot = runtime.request(path, 'stop')
                if not snapshot:
                    raise ValueError('monitor disconnected before stop')
                runtime.wait_stopped(path)
                print('Monitor stopped.')
            else:
                print(json.dumps({key: snapshot[key] for key in ('pid', 'scans', 'updated', 'identity')}))
            return 0
        path = None
        if not args.headless and not args.once:
            path = runtime.directory(args.runtime_dir)
            if not args._owner:
                existing = runtime.request(path, 'status')
                if existing:
                    runtime.check(existing, args)
                    return view(path, args)
        if args.demo:
            hosts = [Host(f'Demo link {i + 1}', f'192.0.2.{i + 1}', 1, 1000) for i in range(5)]
        else:
            hosts = load_hosts(args.hosts)
            required = {'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'} | {h.community_env for h in hosts}
            monitor_credentials.load(required)
            missing = sorted(key for key in required if not os.environ.get(key))
            if missing:
                raise ValueError('set required environment variables: ' + ', '.join(missing))
        if args.headless or args.once:
            return asyncio.run(run(args, hosts))
        if args._owner:
            if not args.demo:
                probe = Snmp()
                probe.close()
            owner = runtime.Owner(path, args)
            try:
                return asyncio.run(run(args, hosts, owner=owner))
            finally:
                owner.close()
        runtime.start(path, args)
        return view(path, args)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, TimeoutError) as error:
        # No raw network exceptions or environment values reach the terminal.
        if isinstance(error, ValueError):
            message = str(error)
        else:
            message = 'file/terminal unavailable or network operation timed out; check paths and connectivity'
        parser.error(message)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        if '--_owner' not in sys.argv:
            raise
        print('Monitor failed unexpectedly; check configuration and restart.', file=sys.stderr)
        sys.exit(1)
