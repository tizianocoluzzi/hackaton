"""Contratti v1 e adattamento dei risultati ML alla dashboard."""
import json
import math
from datetime import datetime

EXCHANGE = 'gnss.events'
RAW_KEY = 'telemetry.raw.v1'
CLASSIFIED_KEY = 'telemetry.classified.v1'
RAW_QUEUE = 'classification.raw.v1'
ALERTS_KEY = 'alerts.snapshot.v1'
ALERTS_QUEUE = 'alerts.classified.v1'
SOURCE_NUMBERS = ('crowd_pos_std_m', 'crowd_accuracy_m', 'estimated_speed_var_ms2',
                  'estimated_heading_var_deg2')
RISK_STATES = ('DETECTED', 'RESOLVED')
NOTIFICATION_STATES = ('DETECTED', 'SENDING', 'DELIVERED', 'ACKNOWLEDGED')
INDICATORS = ('satellites', 'motion', 'crowd', 'map', 'sensors')
LABELS = ('spoofing', 'jamming', 'low_satellites', 'multipath', 'tunnel')
SENSOR_FIELDS = ('latitude', 'longitude', 'speed', 'heading', 'satellites_used',
                 'hdop', 'fix_status', 'crowd_latitude', 'crowd_longitude',
                 'crowd_num_phones', 'estimated_latitude', 'estimated_longitude',
                 'estimated_speed', 'estimated_heading', 'estimated_pos_var_m2')


def clean_json(value):
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def dumps(value):
    return json.dumps(clean_json(value), allow_nan=False, ensure_ascii=False)


def parse_timestamp(value):
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('Timestamp privo di timezone')
    return date


def validate_raw(message):
    if not isinstance(message, dict) or message.get('schema_version') != '1':
        raise ValueError('Atteso contratto v1')
    for field in ('session_id', 'vehicle_id', 'track_id', 'geometry_id', 'timestamp'):
        if not isinstance(message.get(field), str) or not message[field]:
            raise ValueError(f'{field} mancante o non valido')
    sequence = message.get('sequence')
    if type(sequence) is not int or sequence < 0:
        raise ValueError('sequence deve essere un intero non negativo')
    parse_timestamp(message['timestamp'])
    data = message.get('data')
    if not isinstance(data, dict) or set(SENSOR_FIELDS) - set(data):
        raise ValueError('Campi sensoriali mancanti')
    if any(key == 'anomaly_label' or key.startswith(('truth_', '_truth')) for key in data):
        raise ValueError('Label e ground truth non ammesse nel contratto runtime')
    for key in SENSOR_FIELDS:
        value = data[key]
        if key == 'fix_status':
            if value is not None and not isinstance(value, (str, bool)):
                raise ValueError('fix_status non valido')
        elif value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f'{key} deve essere numerico o null')
    for key in SOURCE_NUMBERS:
        value = data.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError(f'{key} deve essere non negativo e finito')
    for key in ('estimated_timestamp', 'crowd_timestamp'):
        if data.get(key) is not None:
            parse_timestamp(data[key])
    if 'estimated_includes_crowd' in data and type(data['estimated_includes_crowd']) is not bool:
        raise ValueError('Provenienza stimata non valida')
    return message


def has_fix(value):
    if value is None:
        return False
    return str(value).upper() not in ('NO_FIX', 'NONE', 'FALSE', '0', 'INVALID', '')


def dashboard_message(raw, indicators=None, prediction=None, thresholds=(.2, .5, .8)):
    data = raw['data']
    warmup = prediction is None
    risk = clean_json(prediction['final_risk']) if prediction is not None else None
    fix = has_fix(data.get('fix_status'))
    probabilities = {label: prediction[f'probability_{label}'] if prediction is not None else None
                     for label in LABELS}
    labels = prediction['predicted_label'].split('+') if prediction is not None else []
    labels = [label for label in labels if label != 'normal']
    if warmup or risk is None:
        status = 'ATTENTION'
    elif not fix:
        status = 'UNRELIABLE'
    else:
        status = ('NOMINAL', 'ATTENTION', 'DEGRADED', 'UNRELIABLE')[sum(risk >= t for t in thresholds)]
    if warmup:
        message = 'Inizializzazione: raccolta dei primi tre campioni'
    elif not fix:
        message = 'Fix GNSS non disponibile'
    elif labels:
        message = 'Possibili anomalie: ' + ', '.join(label.replace('_', ' ') for label in labels)
    else:
        message = 'Navigazione GNSS regolare' if status == 'NOMINAL' else 'Affidabilità GNSS ridotta'
    fallback = ('Attendere il completamento dell’inizializzazione' if warmup else
                'Nessuna azione necessaria' if status == 'NOMINAL' else
                'Verificare la posizione con una fonte alternativa')
    result = {key: raw[key] for key in ('schema_version', 'session_id', 'sequence', 'timestamp', 'vehicle_id')}
    result.update(track_id=raw['track_id'], geometry_id=raw['geometry_id'],
                  source_file=raw.get('source_file'))
    result.update(
        position=dict(latitude=data.get('latitude'), longitude=data.get('longitude'),
                      speed=data['speed'] * 3.6 if data.get('speed') is not None else None,
                      heading=data.get('heading')),
        reference_position=dict(latitude=data.get('estimated_latitude'),
                                longitude=data.get('estimated_longitude'), source='estimated_state'),
        crowd_position=dict(latitude=data.get('crowd_latitude'), longitude=data.get('crowd_longitude'),
                            num_phones=data.get('crowd_num_phones'), source='crowd_mean'),
        gnss=dict(satellites_used=data.get('satellites_used'), hdop=data.get('hdop'),
                  fix_status=fix, fix_type=data.get('fix_status'),
                  signal_quality=data.get('signal_quality'), signal_quality_unit='dB-Hz'),
        analysis=dict(status=status, final_risk=risk,
                      confidence_score=100 * (1 - risk) if risk is not None else None,
                      anomaly_type=' + '.join(labels).upper() if labels else 'NONE',
                      anomaly_types=labels, probabilities=probabilities,
                      indicators={key: indicators.get('risk_' + key) if indicators is not None else None
                                  for key in INDICATORS},
                      warmup=warmup, message=message, fallback=fallback))
    return clean_json(result)
