"""Verifica selezione CSV via Nginx e risultati della nuova sessione su Web STOMP."""
import json
from urllib.parse import quote
from urllib.request import Request, urlopen

from smoke_compose import subscribe, receive, config

BASE = 'http://localhost:' + config.get('FRONTEND_PORT', '8080')


def request(path, data=None):
    req = Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=10) as response:
        return json.load(response)


def main():
    catalog = request('/api/tracks')
    previous = catalog['active']['file_id']
    client = subscribe()
    try:
        for file_id in ['dataset/test/route_0006/scenario_01.csv', 'gnss_tracks/track_000_mixed.csv']:
            selected = request('/api/select', {'file_id': file_id})
            geometry = request('/api/geometry?file=' + quote(file_id, safe=''))
            assert len(geometry['coordinates']) > 2
            received = []
            while len(received) < 3:
                frame = receive(client, 'MESSAGE')
                data = json.loads(frame.split('\n\n', 1)[1].rstrip('\x00\n'))
                if data['session_id'] == selected['session_id']:
                    received.append(data)
            assert [row['sequence'] for row in received] == [0, 1, 2]
            assert all(row['source_file'] == file_id for row in received)
            assert received[0]['analysis']['warmup']
            assert received[2]['analysis']['final_risk'] is not None
            assert received[2]['crowd_position']['num_phones'] > 0
            print('OK:', file_id, '→ nuova sessione, geometria, GPS, media utenti e risultati ML')
    finally:
        request('/api/select', {'file_id': previous})
        client.close()


if __name__ == '__main__':
    main()
