"""Functional SNMP checks on loopback only; no real equipment or credentials."""
import asyncio
import unittest
from unittest.mock import patch

import monitor

try:
    from pyasn1.codec.ber import decoder, encoder
    from pysnmp.proto.api import v2c
    from pysnmp.hlapi.v3arch.asyncio import UdpTransportTarget
except ImportError:
    v2c = None


class Agent(asyncio.DatagramProtocol):
    def __init__(self):
        self.values = {
            '1.3.6.1.2.1.1.1.0': 'loopback agent',
            f'{monitor.IF_TABLE}.3.2': 6,
            f'{monitor.IF_TABLE}.8.2': 1,
            f'{monitor.IF_TABLE}.5.2': 100_000_000,
            f'{monitor.HIGH_SPEED}.2': 1000,
            f'{monitor.IF_TABLE}.2.2': 'eth0',
        }
        self.drop = False
        self.bad_community = False
        self.v1_only = False
        self.versions = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if self.drop:
            return
        request, _ = decoder.decode(data, asn1Spec=v2c.Message())
        version = int(request.getComponentByPosition(0))
        self.versions.append(version)
        if self.v1_only and version != 0:
            return
        response = v2c.apiMessage.get_response(request)
        if self.bad_community:
            v2c.apiPDU.set_error_status(v2c.apiMessage.get_pdu(response), 5)
        bindings = []
        for oid, _ in v2c.apiPDU.get_varbinds(v2c.apiMessage.get_pdu(request)):
            key = str(oid)
            value = self.values.get(key)
            if request.getComponentByPosition(2).getName() == 'get-bulk-request':
                # The only advertised Ethernet interface is ifindex 2.
                key = f'{monitor.IF_TABLE}.3.2' if key == f'{monitor.IF_TABLE}.3' else f'{monitor.IF_TABLE}.4.1'
                value = self.values.get(key, 0)
            typed = v2c.NoSuchInstance('') if value is None else v2c.OctetString(value) if isinstance(value, str) else v2c.Integer(value)
            bindings.append((key, typed))
        v2c.apiPDU.set_varbinds(v2c.apiMessage.get_pdu(response), bindings)
        self.transport.sendto(encoder.encode(response), address)


@unittest.skipIf(v2c is None, 'install requirements.txt to run real loopback SNMP checks')
class SnmpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        self.transport, self.agent = await loop.create_datagram_endpoint(Agent, local_addr=('127.0.0.1', 0))
        self.port = self.transport.get_extra_info('sockname')[1]
        self.client = monitor.Snmp()
        async def target(ip):
            return await UdpTransportTarget.create((ip, self.port), timeout=0.1, retries=0)
        self.client.target = target
        self.env = patch.dict('os.environ', {'TEST_COMMUNITY': 'test-read-only'})
        self.env.start()
        self.host = monitor.Host('loopback', '127.0.0.1', 2, 1000, 'TEST_COMMUNITY')

    async def asyncTearDown(self):
        self.env.stop()
        self.client.close()
        self.transport.close()
        await asyncio.sleep(0)

    async def test_real_packets_speed_down_timeout_and_unsupported(self):
        self.assertEqual((await self.client.sample(self.host))[:2], ('LOW', 100))
        self.agent.values[f'{monitor.IF_TABLE}.5.2'] = 1_000_000_000
        self.assertEqual((await self.client.sample(self.host))[:2], ('OK', 1000))
        self.agent.values[f'{monitor.IF_TABLE}.8.2'] = 2
        self.assertEqual((await self.client.sample(self.host))[0], 'DOWN')
        self.agent.values[f'{monitor.IF_TABLE}.8.2'] = 1
        del self.agent.values[f'{monitor.IF_TABLE}.5.2']
        self.assertEqual((await self.client.sample(self.host))[:2], ('OK', 1000))
        del self.agent.values[f'{monitor.HIGH_SPEED}.2']
        self.assertEqual((await self.client.sample(self.host))[0], 'UNKNOWN')
        self.agent.drop = True
        self.assertEqual((await self.client.sample(self.host))[0], 'UNREACHABLE')

    async def test_wrong_interface_and_protocol_error_not_down(self):
        self.agent.values[f'{monitor.IF_TABLE}.3.2'] = 71  # Wireless.
        self.assertEqual((await self.client.sample(self.host))[0], 'UNKNOWN')
        self.agent.bad_community = True
        self.assertEqual((await self.client.sample(self.host))[0], 'UNKNOWN')

    async def test_airmax_megabit_units_in_ifspeed_are_corrected(
            self):
        self.agent.values[f'{monitor.IF_TABLE}.5.2'] = 1000  # AirOS 8 LAN: Mbps in the bps field.
        self.agent.values[monitor.HIGH_SPEED + '.2'] = 1000
        self.assertEqual((await self.client.sample(self.host))[:2], ('OK', 1000))
        self.host = monitor.Host('loopback', '127.0.0.1', 2, 100, 'TEST_COMMUNITY')
        self.assertEqual((await self.client.sample(self.host))[:2], ('OK', 1000))

    async def test_v1_only_agent_is_detected_and_cached(self):
        self.agent.v1_only = True
        self.agent.values[f'{monitor.IF_TABLE}.5.2'] = 1_000_000_000
        self.assertEqual((await self.client.sample(self.host))[:2], ('OK', 1000))
        self.agent.versions.clear()
        self.assertEqual((await self.client.sample(self.host))[:2], ('OK', 1000))
        self.assertEqual(self.agent.versions, [0])

    async def test_discover_returns_only_ethernet_index_and_name(self):
        self.assertEqual(await self.client.discover('127.0.0.1', 'test-read-only'), ['2\teth0'])


if __name__ == '__main__':
    unittest.main()
