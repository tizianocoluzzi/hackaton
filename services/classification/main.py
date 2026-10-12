"""Indicatori sul prefisso della sessione e inferenza delle due Random Forest."""
import json
import logging
import os
from pathlib import Path
import time

import joblib
import pandas as pd
import pika
from shared.broker import connect, properties
from shared.contracts import (EXCHANGE, CLASSIFIED_KEY, RAW_QUEUE, dashboard_message,
                              dumps, has_fix, parse_timestamp, validate_raw)
from shared.localization import Localizer
from shared.inference import predict
from shared.preprocessing import RiskProcessor

LOG = logging.getLogger(__name__)


class Classifier:
    def __init__(self, processor, models, data_root, thresholds=(.2, .5, .8)):
        self.processor = processor
        self.models = models
        self.data_root = Path(data_root).resolve()
        self.thresholds = thresholds
        if len(thresholds) != 3 or not 0 < thresholds[0] < thresholds[1] < thresholds[2] < 1:
            raise ValueError('Servono tre soglie ordinate in (0, 1)')
        self.sessions = {}
        self.geometries = {}
        self.retired_sessions = set()

    def geometry(self, geometry_id):
        if geometry_id not in self.geometries:
            path = (self.data_root / geometry_id).resolve()
            if not path.is_relative_to(self.data_root) or path.suffix != '.csv':
                raise ValueError('geometry_id non valido')
            try:
                self.geometries[geometry_id] = pd.read_csv(path)
            except OSError as error:
                raise ValueError(f'Geometria non disponibile: {geometry_id}') from error
        return self.geometries[geometry_id]

    def process(self, raw):
        validate_raw(raw)
        vehicle = raw['vehicle_id']
        if (vehicle, raw['session_id']) in self.retired_sessions:
            return None
        state = self.sessions.get(vehicle)
        if state is None or state['id'] != raw['session_id']:
            if state is not None:
                self.retired_sessions.add((vehicle, state['id']))
            state = dict(localizer=Localizer(), id=raw['session_id'], rows=[], sequence=-1, delivered=-1,
                         result=None, geometry_id=raw['geometry_id'])
            self.sessions[vehicle] = state
        sequence = raw['sequence']
        if sequence <= state['delivered']:
            return None
        if sequence == state['sequence']:
            return state['result']  # Ritenta un risultato la cui pubblicazione è fallita.
        if sequence < state['sequence']:
            return None
        if state['geometry_id'] != raw['geometry_id']:
            raise ValueError('Geometria cambiata nella stessa sessione')
        if sequence != state['sequence'] + 1:
            LOG.warning('Gap di sequenza per %s: %s -> %s', vehicle, state['sequence'], sequence)
        rows = state['rows']
        if rows:
            step = (parse_timestamp(raw['timestamp']) - parse_timestamp(rows[-1]['timestamp'])).total_seconds()
            if len(rows) >= 2:
                expected = (parse_timestamp(rows[-1]['timestamp']) - parse_timestamp(rows[-2]['timestamp'])).total_seconds()
                if abs(step - expected) > max(.001, abs(expected) * .01):
                    LOG.warning('Timestamp non regolari per %s', vehicle)
            if step <= 0:
                raise ValueError('Timestamp non crescente')
        row = dict(raw['data'], timestamp=raw['timestamp'])
        row['fix_status'] = str(row['fix_status']) if has_fix(row['fix_status']) else 'NO_FIX'
        prefix = rows + [row]
        if len(prefix) < 3:
            result = dashboard_message(raw, thresholds=self.thresholds)
        else:
            features = self.processor.transform(pd.DataFrame(prefix), self.geometry(raw['geometry_id'])).tail(1)
            prediction = predict(self.models, features).iloc[0]
            result = dashboard_message(raw, features.iloc[0], prediction, self.thresholds)
        result['localization'] = state['localizer'].update(raw, result['analysis'])
        result['traffic'] = raw.get('traffic', {})
        result['network_id'] = raw.get('network_id')
        state.update(rows=prefix, sequence=sequence, result=result)
        return result

    def delivered(self, raw):
        self.sessions[raw['vehicle_id']]['delivered'] = raw['sequence']


def consume(classifier, connection):
    channel = connection.channel()
    channel.confirm_delivery()
    channel.basic_qos(prefetch_count=1)

    def on_message(ch, method, properties_in, body):
        try:
            raw = json.loads(body)
            result = classifier.process(raw)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            LOG.warning('Messaggio non valido inviato in DLQ: %s', error)
            ch.basic_reject(method.delivery_tag, requeue=False)
            return
        if result is not None:
            # Un risultato senza browser collegati può non avere destinazioni.
            ch.basic_publish(exchange=EXCHANGE, routing_key=CLASSIFIED_KEY,
                             body=dumps(result), properties=properties(), mandatory=False)
            classifier.delivered(raw)
        ch.basic_ack(method.delivery_tag)

    channel.basic_consume(queue=RAW_QUEUE, on_message_callback=on_message, auto_ack=False)
    LOG.info('Classificazione pronta')
    channel.start_consuming()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    models_dir = Path(os.getenv('MODELS_DIR', '/models'))
    processor = RiskProcessor.load(models_dir / 'preprocessing.joblib')
    models = joblib.load(models_dir / 'random_forest_models.joblib')
    thresholds = tuple(float(t) for t in os.getenv('RISK_THRESHOLDS', '0.2,0.5,0.8').split(','))
    classifier = Classifier(processor, models, os.getenv('DATA_ROOT', '/data'), thresholds)
    while True:
        connection = None
        try:
            connection = connect()
            consume(classifier, connection)
        except (pika.exceptions.AMQPError, OSError) as error:
            LOG.warning('Connessione interrotta; nuovo tentativo: %s', error)
        finally:
            if connection is not None and connection.is_open:
                connection.close()
        time.sleep(2)


if __name__ == '__main__':
    main()
