"""Replay CSV temporizzato; non genera dati e non addestra modelli."""
import csv
import json
import logging
import math
import os
from pathlib import Path
import time
import threading
from uuid import uuid4

import pika
from shared.broker import connect, properties
from shared.contracts import EXCHANGE, RAW_KEY, dumps, parse_timestamp, validate_raw

from services.simulation.scenario import fleet
from services.simulation.control import ReplayControl, make_server

LOG = logging.getLogger(__name__)


def replay_batches(path, vehicle_id, geometry_id, session_id, root):
    """Optional data-only fleet descriptor; preserve the original switch fixture."""
    descriptor = path.with_suffix('.scenario.json')
    if not descriptor.is_file():
        for message in replay_messages(path, vehicle_id, geometry_id, session_id):
            message['source_file'] = path.relative_to(root).as_posix()
            yield list(fleet(message)) if path.name == 'demo_switch.csv' else [message]
        return
    spec = json.loads(descriptor.read_text())
    streams = []
    for vehicle in spec['vehicles']:
        source = (path.parent / vehicle['csv']).resolve()
        geometry = (path.parent / vehicle['geometry']).resolve()
        if not source.is_relative_to(root) or not geometry.is_relative_to(root):
            raise ValueError('Percorso scenario non valido')
        streams.append(replay_messages(source, vehicle['id'], geometry.relative_to(root).as_posix(), session_id))
    from itertools import zip_longest
    start = None
    for batch in zip_longest(*streams):
        if any(message is None for message in batch):
            raise ValueError('Le tracce dello scenario devono avere uguale durata')
        if len({message['timestamp'] for message in batch}) != 1:
            raise ValueError('Le tracce dello scenario devono essere sincronizzate')
        start = start if start is not None else parse_timestamp(batch[0]['timestamp']).timestamp()
        for message, vehicle in zip(batch, spec['vehicles']):
            message['source_file'] = path.relative_to(root).as_posix()
            message['network_id'] = spec['network_id']
            message['traffic'] = dict(level=vehicle.get('level', 0), direction=vehicle.get('direction', 1),
                route=vehicle['route'], transits=[dict(zone_id=t['zone_id'], enter=start+t['enter'], exit=start+t['exit'])
                                               for t in vehicle['transits']])
        yield list(batch)


def sensor_value(value):
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        return value


def replay_messages(path, vehicle_id, geometry_id, session_id=None):
    session_id = session_id or str(uuid4())
    with Path(path).open(newline='') as stream:
        for sequence, row in enumerate(csv.DictReader(stream)):
            timestamp = parse_timestamp(row['timestamp']).isoformat().replace('+00:00', 'Z')
            data = {key: sensor_value(value) for key, value in row.items()
                    if key not in ('timestamp', 'anomaly_label') and not key.startswith(('truth_', '_truth'))}
            # Generated CSV sources are independent and sampled on the row clock.
            data.setdefault('estimated_timestamp', timestamp)
            data.setdefault('crowd_timestamp', timestamp)
            if isinstance(data.get('estimated_includes_crowd'), str):
                value = data['estimated_includes_crowd'].lower()
                if value not in ('true', 'false'):
                    raise ValueError('Provenienza sensori non valida')
                data['estimated_includes_crowd'] = value == 'true'
            data.setdefault('estimated_includes_crowd', False)
            yield validate_raw(dict(schema_version='1', session_id=session_id, vehicle_id=vehicle_id,
                                    track_id=Path(path).stem, sequence=sequence, timestamp=timestamp,
                                    geometry_id=geometry_id, data=data))


class Publisher:
    def __init__(self):
        self.connection = None
        self.channel = None

    def publish(self, message, cancelled=lambda: False):
        while not cancelled():
            try:
                if self.connection is None or self.connection.is_closed:
                    self.connection = connect()
                    self.channel = self.connection.channel()
                    self.channel.confirm_delivery()
                self.channel.basic_publish(exchange=EXCHANGE, routing_key=RAW_KEY,
                                           body=dumps(message), properties=properties(), mandatory=True)
                return True
            except (pika.exceptions.AMQPError, OSError) as error:
                LOG.warning('Pubblicazione non confermata; riconnessione: %s', error)
                if self.connection is not None and self.connection.is_open:
                    self.connection.close()
                self.connection = None
                time.sleep(2)

    def wait(self, seconds, cancelled=lambda: False):
        # Mantiene attivi gli heartbeat anche con replay rallentato.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not cancelled():
            delay = min(.1, deadline - time.monotonic())
            try:
                if self.connection is not None and self.connection.is_open:
                    self.connection.sleep(delay)
                else:
                    time.sleep(delay)
            except (pika.exceptions.AMQPError, OSError):
                self.connection = None


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    path = Path(os.getenv('CSV_PATH', '/data/gnss_tracks/track_000_mixed.csv'))
    control = ReplayControl(os.getenv('DATA_ROOT', '/data'), path, os.getenv('GEOMETRY_ID'))
    server = make_server(control)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    speed = float(os.getenv('REPLAY_SPEED', '1'))
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError('REPLAY_SPEED deve essere positivo e finito')
    loop = os.getenv('REPLAY_LOOP', 'true').lower() in ('true', '1', 'yes')
    publisher = Publisher()
    while True:
        active = control.snapshot()
        path = control.root / active['file_id']
        cancelled = lambda: control.changed(active['session_id'])
        previous = None
        published_at = None
        count = 0
        for batch in replay_batches(path, os.getenv('VEHICLE_ID', 'TRAIN_001'),
                                   active['geometry_id'], active['session_id'], control.root):
            message = batch[0]
            timestamp = parse_timestamp(message['timestamp'])
            if previous is not None:
                interval = (timestamp - previous).total_seconds() / speed
                publisher.wait(max(0, interval - (time.monotonic() - published_at)), cancelled)
            if cancelled() or not all(publisher.publish(item, cancelled) for item in batch):
                break
            published_at = time.monotonic()
            previous = timestamp
            count += 1
            if count == 1:
                LOG.info('Replay sessione %s: %s, velocità %sx', message['session_id'], path, speed)
        if cancelled():
            continue
        LOG.info('Traccia completata: %s campioni', count)
        if loop and count:
            publisher.wait(1 / speed, cancelled)
            control.repeat(active['session_id'])
        else:
            # Mantiene disponibile il selettore anche al termine di un replay singolo.
            control.wait_for_change(active['session_id'])


if __name__ == '__main__':
    main()
