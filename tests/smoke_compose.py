"""Con stack attivo: due client Web STOMP ricevono gli stessi risultati ML.

Eseguire dalla radice: python tests/smoke_compose.py
Richiede websocket-client (requirements-test.txt).
"""
import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4

import websocket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.simulation.main import replay_messages
from shared.contracts import dumps

config = {}
if (ROOT / '.env').exists():
    for line in (ROOT / '.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            config[key] = value.strip().strip('"\'')
config.update(os.environ)
url = config.get('WEB_STOMP_URL', 'ws://localhost:15674/ws')
admin = config.get('ADMIN_USER', 'admin') + ':' + config.get('ADMIN_PASSWORD', 'admin_demo')
management = 'http://localhost:' + config.get('MANAGEMENT_PORT', '15672')


def api(path, body=None):
    request = Request(management + '/api/' + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': 'Basic ' + base64.b64encode(admin.encode()).decode(),
                               'Content-Type': 'application/json'})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def receive(client, command):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        frame = client.recv().lstrip('\n')
        if frame.startswith('ERROR'):
            raise AssertionError(frame)
        if frame.startswith(command + '\n'):
            return frame
    raise TimeoutError(command)


def subscribe():
    client = websocket.create_connection(url, timeout=20, subprotocols=['v12.stomp'])
    client.send('CONNECT\naccept-version:1.2\nhost:/\nlogin:' + config.get('FRONTEND_USER', 'frontend') +
                '\npasscode:' + config.get('FRONTEND_PASSWORD', 'frontend_demo') + '\nheart-beat:0,0\n\n\x00')
    receive(client, 'CONNECTED')
    client.send('SUBSCRIBE\nid:smoke\ndestination:/exchange/gnss.events/telemetry.classified.v1\n'
                'ack:auto\ndurable:false\nauto-delete:true\nexclusive:true\nreceipt:ready\n\n\x00')
    receive(client, 'RECEIPT')
    return client


def publish(raw):
    result = api('exchanges/%2F/gnss.events/publish',
                 dict(properties={'content_type': 'application/json', 'delivery_mode': 2},
                      routing_key='telemetry.raw.v1', payload=dumps(raw), payload_encoding='string'))
    assert result['routed'], result


def main():
    clients = []
    try:
        clients = [subscribe(), subscribe()]
        session = str(uuid4())
        messages = replay_messages(ROOT / 'gnss_tracks/track_000_mixed.csv', 'SMOKE_CHECK',
                                   'gnss_tracks/track_000_mixed.geometry.csv', session)
        for _ in range(3):
            publish(next(messages))
        results = []
        for client in clients:
            received = []
            while len(received) < 3:
                frame = receive(client, 'MESSAGE')
                body = frame.split('\n\n', 1)[1].rstrip('\x00\n')
                payload = json.loads(body)
                if payload.get('session_id') == session:
                    received.append(payload)
            results.append(received)
        assert results[0] == results[1], 'I due client non hanno ricevuto gli stessi risultati'
        assert [r['sequence'] for r in results[0]] == [0, 1, 2]
        assert results[0][0]['analysis']['confidence_score'] is None
        assert results[0][2]['analysis']['final_risk'] is not None
        assert not results[0][2]['analysis']['warmup']
        publish({'schema_version': 'invalid', 'smoke_test_id': session})
        found = False
        for _ in range(20):
            dlq = api('queues/%2F/classification.raw.dlq/get',
                      dict(count=100, ackmode='ack_requeue_true', encoding='auto'))
            if any(session in entry['payload'] for entry in dlq):
                found = True
                break
            time.sleep(.25)
        assert found, 'Messaggio non valido non trovato in DLQ'
        print('OK: raw → classificazione ML → due client Web STOMP identici; warmup e DLQ verificati.')
    finally:
        for client in clients:
            client.close()


if __name__ == '__main__':
    main()
