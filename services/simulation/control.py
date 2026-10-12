"""Catalogo e controllo del replay per la dashboard locale del mockup."""
import csv
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from shared.contracts import SENSOR_FIELDS, dumps


class ReplayControl:
    def __init__(self, root, initial, geometry_override=None):
        self.root = Path(root).resolve()
        self.tracks = {}
        for folder in ('gnss_tracks', 'dataset'):
            for path in sorted((self.root / folder).rglob('*.csv')):
                if path.name == 'geometry.csv' or path.name.endswith(('.truth.csv', '.geometry.csv', '.vehicle.csv')):
                    continue
                with path.open(newline='') as stream:
                    fields = csv.DictReader(stream).fieldnames or []
                if not set(SENSOR_FIELDS + ('timestamp',)) <= set(fields):
                    continue
                geometry = path.parent / 'geometry.csv'
                if not geometry.is_file():
                    geometry = path.with_suffix('.geometry.csv')
                if geometry.is_file():
                    self.tracks[path.relative_to(self.root).as_posix()] = geometry
        initial = Path(initial).resolve().relative_to(self.root).as_posix()
        if geometry_override:
            geometry = (self.root / geometry_override).resolve()
            if not geometry.is_relative_to(self.root) or not geometry.is_file():
                raise ValueError('Geometria iniziale non valida')
            self.tracks[initial] = geometry
        self.condition = threading.Condition()
        self.current = None
        self.select(initial)

    def catalog(self):
        return [{'id': key, 'label': 'Demo ferroviaria · scambio e tre mezzi' if key.endswith('/demo_switch.csv') else key} for key in self.tracks]

    def snapshot(self):
        with self.condition:
            return dict(self.current)

    def select(self, file_id):
        if not isinstance(file_id, str) or file_id not in self.tracks:
            raise ValueError('Selezionare un CSV del catalogo')
        # Controlla la geometria prima di accettare il cambio.
        self.geometry(file_id)
        with self.condition:
            self.current = dict(file_id=file_id, session_id=str(uuid4()),
                                geometry_id=self.tracks[file_id].relative_to(self.root).as_posix())
            self.condition.notify_all()
            return dict(self.current)

    def geometry(self, file_id):
        if file_id not in self.tracks:
            raise ValueError('File non disponibile')
        with self.tracks[file_id].open(newline='') as stream:
            coordinates = [[float(row['latitude']), float(row['longitude'])] for row in csv.DictReader(stream)]
        if len(coordinates) < 2 or not all(math.isfinite(v) for point in coordinates for v in point):
            raise ValueError('Geometria non valida')
        return {'file_id': file_id, 'coordinates': coordinates, 'source': 'track_geometry'}

    def changed(self, session_id):
        return self.snapshot()['session_id'] != session_id

    def repeat(self, session_id):
        with self.condition:
            if self.current['session_id'] == session_id:
                self.current = dict(self.current, session_id=str(uuid4()))
                self.condition.notify_all()

    def wait_for_change(self, session_id):
        with self.condition:
            self.condition.wait_for(lambda: self.current['session_id'] != session_id)


def make_server(control, host='0.0.0.0', port=8081):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value):
            body = dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            try:
                if url.path == '/api/tracks':
                    self.respond(200, {'tracks': control.catalog(), 'active': control.snapshot()})
                elif url.path == '/api/geometry':
                    self.respond(200, control.geometry(parse_qs(url.query).get('file', [''])[0]))
                else:
                    self.respond(404, {'error': 'Risorsa non trovata'})
            except (ValueError, KeyError) as error:
                self.respond(400, {'error': str(error)})

        def do_POST(self):
            if self.path != '/api/select':
                self.respond(404, {'error': 'Risorsa non trovata'})
                return
            try:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Richiesto JSON')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('Richiesta non valida')
                data = json.loads(self.rfile.read(length))
                self.respond(202, control.select(data.get('file_id')))
            except (ValueError, AttributeError, TypeError) as error:
                self.respond(400, {'error': str(error)})

    return ThreadingHTTPServer((host, port), Handler)
