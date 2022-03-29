#!/usr/bin/env python3
"""
jsolar.py

This program will listen for VBus packets arriving via serial port, and serve
the results via HTTP, in JSON format.

"""

# -*- coding: utf-8 -*-
#
# Copyright 2020 Jonathan McCrohan <jmccrohan@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.

import sys
import asyncio
import json
import logging
import signal
import functools
from datetime import datetime
import serial_asyncio
from aiohttp import web
from configobj import ConfigObj
from validate import Validator, ValidateError
from pyvbus.vbuspacket import VBUSPacket, VBUSPacketException


def dummy_serial():
    global parsed_datagram

    buffer = bytearray.fromhex(
        'aa10007842100001071d39007a01014a47023822045800000000007f44060000013400000000007f00000003007c4a0000000134'
    )
    parsed_datagram = ParseVbusPacket(buffer).get_parse_result()


async def setup_serial():
    reader, _ = await serial_asyncio.open_serial_connection(url=config['serial'].get('port'), baudrate=config['serial'].get('baud'))
    received = data_received(reader)
    await asyncio.wait([received])


async def data_received(r):
    global parsed_datagram

    await r.readuntil(b'\xaa')  # message start
    while True:
        buffer = bytearray(b'\xaa')
        buffer.extend(await r.readuntil(b'\xaa'))
        buffer = buffer[0:len(buffer)-1]  # last entry contains next message start
        if (len(buffer) >= 5 and buffer[5] == 0x10):  # Only process protocol version 1.0

            parsed_datagram = ParseVbusPacket(buffer).get_parse_result()

def parse_config():


    global config
    validator = Validator()
    configspec = """
    [device]
    name = string(default='unknown')
    deviceid = string
    description = string(default='description')
    [logging]
    loglevel = option('DEBUG','INFO','WARNING','ERROR','CRITICAL',default='INFO')
    [serial]
    port = string(default='/dev/ttyS0')
    baud = integer(default=9600)
    parity = string(default='N')
    databits = integer(default=8)
    stopbits = integer(default=1)
    [server]
    address = string(default='localhost')
    port = integer(min=1, max=65535, default=8088)
    [sensors]
    [[__many__]]
    description = string(default='description')
    offset = integer(default=0)
    size = integer(default=1)
    factor = float(default=0.1)
    position = integer(min=0, max=7, default=0)
    enabled = boolean(default=0)
    type = string(default='raw')
    metrics = string(default='gague')
    """.splitlines()

    try:
        config = ConfigObj('jsolar.ini', configspec=configspec, file_error=True)
    except IOError as e:
        sys.exit("ERROR: "+str(e))

    config.validate(validator, preserve_errors=True)
    for sensor in config['sensors']:
        if not config['sensors'][sensor].get('enabled'):
            config['sensors'].pop(sensor, None)  # discard disabled sensors



class ParseVbusPacket:
    """
    The ParseVbusPacket class processes a buffer containing a VBus packet and
    creates an internal dict (_parsed_datagram) containing a list of enabled
    VBus sensors. Sensor configuration is taken from the configuration file.
    """
    def __init__(self, buffer):
        vbus_packet = VBUSPacket(buffer)
        self._parsed_datagram = {'device': config['device'].get('name')}
        self._parsed_datagram['timestamp'] = datetime.now().replace(
            microsecond=0).astimezone().isoformat()
        for sensor in config['sensors']:
            sensor_name = sensor
            sensor_type = config['sensors'][sensor].get('type')
            sensor_offset = config['sensors'][sensor].get('offset')
            sensor_size = config['sensors'][sensor].get('size')
            sensor_position = config['sensors'][sensor].get('position')
            sensor_factor = config['sensors'][sensor].get('factor')
            sensor_value = None
            if sensor_type == 'raw':
                sensor_value = vbus_packet.GetRawValue(sensor_offset, sensor_size)
            elif sensor_type == 'temperature':
                sensor_value = vbus_packet.GetTemperatureValue(
                    sensor_offset, sensor_size, sensor_factor)
            elif sensor_type == 'bit':
                sensor_value = vbus_packet.GetBitValue(sensor_offset, sensor_position)
            elif sensor_type == 'time':
                sensor_value = vbus_packet.GetTimeValue(sensor_offset, sensor_size)
            else:  # sensor_type == 'raw'
                sensor_value = vbus_packet.GetRawValue(sensor_offset, sensor_size)
            self._parsed_datagram[sensor_name] = sensor_value

    def print_json_datagram(self):
        print(json.dumps(self._parsed_datagram, indent=4))

    def get_parse_result(self):
        return self._parsed_datagram


def jsolar_app():
    async def json_handler(request):
        if parsed_datagram is None: # Return HTTP/503 if no message received yet
            raise web.HTTPServiceUnavailable
        return web.json_response(parsed_datagram, dumps=functools.partial(json.dumps, indent=4))

    async def prometheus_handler(request):
        if parsed_datagram is None: # Return HTTP/503 if no message received yet
            raise web.HTTPServiceUnavailable
        response=''
        for metric in parsed_datagram:
            if metric not in ['device','timestamp']:
                metric_type = config['sensors'][metric].get('type')
                response = response + \
                    '# HELP ' + metric + ' ' + config['sensors'][metric].get('description') + '\n' + \
                    '# TYPE ' + metric + ' ' + config['sensors'][metric].get('metrics') + '\n' + \
                    metric + ' ' + str(parsed_datagram[metric]) + '\n'
        return web.Response(text=response)

    app = web.Application()
    app.add_routes([web.get('/', json_handler)])
    app.add_routes([web.get('/metrics', prometheus_handler)])
    runner = web.AppRunner(app)
    return runner


def web_server():
    app_runner = jsolar_app()
    event_loop.run_until_complete(app_runner.setup())
    site4 = web.TCPSite(app_runner, '0.0.0.0', config['server'].get('port'))
    site6 = web.TCPSite(app_runner, '::', config['server'].get('port'))

    event_loop.run_until_complete(site4.start())
    event_loop.run_until_complete(site6.start())


def handle_signals():
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        event_loop.add_signal_handler(s, lambda: asyncio.create_task(shutdown(s)))

    async def shutdown(sig):
        print('caught {0}'.format(sig.name))
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        try:
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            event_loop.stop()


def main():
    global parsed_datagram
    parsed_datagram = None

    global event_loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    parse_config()
    dummy_serial()

    handle_signals()
    web_server()
    #event_loop.run_until_complete(setup_serial())
    event_loop.run_forever()


if __name__ == "__main__":
    main()
