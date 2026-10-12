"""Durable snapshots and transactional outbox; broker reconnects are idempotent."""
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from functools import wraps

import pika
from shared.broker import connect, properties
from shared.contracts import EXCHANGE, ALERTS_KEY, ALERTS_QUEUE, dumps
from services.alerts.engine import AlertEngine

LOG = logging.getLogger(__name__)


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class Store:
    def __init__(self, path, engine):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.engine = engine
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, body TEXT)')
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if row:
            engine.state = json.loads(row[0])

    @synchronized
    def accept(self, message, receiver):
        previous = dumps(self.engine.state)
        try:
            self.engine.update(message)
            self.engine.dispatch(message['session_id'], receiver)
            snapshot = self.engine.snapshot(message['session_id'])
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (dumps(self.engine.state),))
                self.db.execute('INSERT INTO outbox(body) VALUES (?)', (dumps(snapshot),))
        except Exception:
            self.engine.state = json.loads(previous)
            raise

    @synchronized
    def flush(self, channel):
        for key, body in self.db.execute('SELECT id, body FROM outbox ORDER BY id').fetchall():
            channel.basic_publish(exchange=EXCHANGE, routing_key=ALERTS_KEY, body=body,
                                  properties=properties(), mandatory=False)
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE id=?', (key,))


    @synchronized
    def snapshot(self, sid):
        if sid not in self.engine.state['sessions']:
            raise KeyError('Sessione non disponibile')
        return json.loads(dumps(self.engine.snapshot(sid)))

    @synchronized
    def acknowledge(self, sid, aid, recipient):
        session = self.engine.state['sessions'][sid]
        notification = session['alerts'][aid]['notifications'][recipient]
        if not notification['confirmed']:
            raise ValueError('Consegna non ancora confermata')
        self.engine.receipt(sid, aid, recipient, acknowledged=True)
        session['revision'] += 1
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (dumps(self.engine.state),))
            self.db.execute('INSERT INTO outbox(body) VALUES (?)', (dumps(self.engine.snapshot(sid)),))
        return self.snapshot(sid)


def make_server(store, host='0.0.0.0', port=8082):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value):
            body = dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                sid = parse_qs(urlsplit(self.path).query)['session'][0]
                self.respond(200, store.snapshot(sid))
            except (KeyError, ValueError):
                self.respond(404, dict(error='Sessione non disponibile'))

        def do_POST(self):
            if urlsplit(self.path).path != '/api/alerts/ack':
                self.respond(404, dict(error='Risorsa non disponibile'))
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('Richiesta non valida')
                data = json.loads(self.rfile.read(length))
                self.respond(200, store.acknowledge(data['session_id'], data['alert_id'], data['recipient']))
            except (KeyError, ValueError, TypeError):
                self.respond(400, dict(error='Presa in carico non consentita: verificare sessione e consegna'))
    return ThreadingHTTPServer((host, port), Handler)


def main():
    logging.basicConfig(level=logging.INFO)
    network = json.loads(Path(os.getenv('NETWORK_PATH', '/network/demo.json')).read_text())
    engine = AlertEngine(network, float(os.getenv('ALERT_HORIZON_S', '300')),
                         float(os.getenv('CLEARANCE_STABILITY_S', '10')),
                         float(os.getenv('CLEARANCE_MARGIN_M', '10')))
    for path in sorted(Path(os.getenv('NETWORK_PATH', '/network/demo.json')).parent.glob('*.network.json')):
        engine.register_network(json.loads(path.read_text()))
    store = Store(os.getenv('ALERT_DB', '/state/alerts.sqlite'), engine)
    threading.Thread(target=make_server(store).serve_forever, daemon=True).start()
    failures = int(os.getenv('SIMULATED_DELIVERY_FAILURES', '1'))
    # Explicit simulated endpoint receipt. AMQP publisher-confirm is NOT delivery to a vehicle.
    receiver = lambda alert, recipient, attempt: attempt > failures
    while True:
        connection = None
        try:
            connection = connect()
            channel = connection.channel()
            channel.confirm_delivery()
            channel.basic_qos(prefetch_count=1)
            store.flush(channel)

            def flush_pending():
                store.flush(channel)
                connection.call_later(1, flush_pending)
            connection.call_later(1, flush_pending)

            def receive(ch, method, props, body):
                try:
                    message = json.loads(body)
                    store.accept(message, receiver)
                except (ValueError, KeyError, TypeError) as error:
                    LOG.warning('Contratto allarmi non valido: %s', error)
                    ch.basic_reject(method.delivery_tag, requeue=False)
                    return
                store.flush(ch)
                ch.basic_ack(method.delivery_tag)

            channel.basic_consume(ALERTS_QUEUE, receive, auto_ack=False)
            channel.start_consuming()
        except (pika.exceptions.AMQPError, OSError):
            LOG.exception('Riconnessione allarmi')
        finally:
            if connection and connection.is_open:
                connection.close()
        time.sleep(2)


if __name__ == '__main__':
    main()
