"""Verifiche dei contratti e della parità tra replay e preprocessing batch."""
import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import joblib
import numpy as np
import pandas as pd

from services.classification.main import Classifier, consume
from services.simulation.main import replay_messages
from shared.contracts import dashboard_message, dumps, validate_raw, INDICATORS, LABELS
from shared.inference import predict
from shared.preprocessing import RiskProcessor

ROOT = Path(__file__).resolve().parents[1]
TRACK = ROOT / 'gnss_tracks/track_000_mixed.csv'
GEOMETRY = 'gnss_tracks/track_000_mixed.geometry.csv'


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.processor = RiskProcessor.load(ROOT / 'models/preprocessing.joblib')
        cls.models = joblib.load(ROOT / 'models/random_forest_models.joblib')
        cls.messages = list(itertools.islice(replay_messages(TRACK, 'TRAIN_001', GEOMETRY, 'session-a'), 45))
        cls.frame = pd.read_csv(TRACK).head(45)
        cls.geometry = pd.read_csv(ROOT / GEOMETRY)

    def classifier(self):
        return Classifier(self.processor, self.models, ROOT)

    def test_replay_excludes_labels_truth_and_preserves_sensors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.csv'
            frame = self.frame.head(3).copy()
            frame['truth_latitude'] = 999
            frame['imu_acceleration'] = [1.2, np.nan, 0.3]
            frame.to_csv(path, index=False)
            messages = list(replay_messages(path, 'TRAIN', GEOMETRY))
        for message in messages:
            validate_raw(message)
            self.assertNotIn('anomaly_label', message['data'])
            self.assertNotIn('truth_latitude', message['data'])
        self.assertIsNone(messages[1]['data']['imu_acceleration'])
        self.assertEqual(messages[0]['data']['imu_acceleration'], 1.2)
        self.assertNotEqual(messages[0]['session_id'], next(replay_messages(TRACK, 'TRAIN', GEOMETRY))['session_id'])
        self.assertEqual([m['sequence'] for m in messages], [0, 1, 2])

    def test_offline_schema_and_label_optional(self):
        labeled = self.processor.transform(self.frame, self.geometry)
        unlabeled = self.processor.transform(self.frame.drop(columns='anomaly_label'), self.geometry)
        self.assertEqual(list(labeled), ['timestamp'] + ['risk_' + name for name in INDICATORS] + ['anomaly_label'])
        pd.testing.assert_frame_equal(labeled.drop(columns='anomaly_label'), unlabeled)
        pd.testing.assert_frame_equal(predict(self.models, labeled), predict(self.models, unlabeled))
        self.assertFalse(hasattr(self.processor, 'final_risk'))

    def test_streaming_prefix_matches_batch(self):
        classifier = self.classifier()
        batch = self.processor.transform(self.frame, self.geometry)
        for index, raw in enumerate(self.messages):
            result = classifier.process(raw)
            if index < 2:
                self.assertTrue(result['analysis']['warmup'])
                self.assertIsNone(result['analysis']['final_risk'])
                self.assertIsNone(result['analysis']['confidence_score'])
                self.assertEqual(result['analysis']['status'], 'ATTENTION')
            else:
                expected = batch.iloc[index]
                actual = result['analysis']['indicators']
                for name in INDICATORS:
                    if pd.isna(expected['risk_' + name]):
                        self.assertIsNone(actual[name])
                    else:
                        self.assertAlmostEqual(actual[name], expected['risk_' + name], places=10)
                prediction = predict(self.models, batch.iloc[[index]]).iloc[0]
                self.assertAlmostEqual(result['analysis']['final_risk'], prediction.final_risk)
            classifier.delivered(raw)
            self.assertIsNone(classifier.process(raw))

    def test_retry_and_session_vehicle_isolation(self):
        classifier = self.classifier()
        first = self.messages[0]
        result = classifier.process(first)
        self.assertIs(classifier.process(first), result)
        classifier.delivered(first)
        for raw in self.messages[1:3]:
            classifier.process(raw)
            classifier.delivered(raw)
        other = dict(first, vehicle_id='TRAIN_002')
        self.assertTrue(classifier.process(other)['analysis']['warmup'])
        self.assertEqual(len(classifier.sessions['TRAIN_001']['rows']), 3)
        changed = dict(first, session_id='session-b')
        self.assertTrue(classifier.process(changed)['analysis']['warmup'])
        self.assertEqual(len(classifier.sessions['TRAIN_001']['rows']), 1)

    def test_no_fix_multilabel_units_and_status(self):
        raw = copy.deepcopy(self.messages[0])
        raw['data'].update(speed=10, fix_status='NO_FIX', latitude=None, longitude=None)
        prediction = {'final_risk': .28, 'predicted_label': 'spoofing+multipath',
                      **{'probability_' + key: .7 for key in LABELS}}
        result = dashboard_message(raw, {}, prediction)
        self.assertEqual(result['analysis']['status'], 'UNRELIABLE')
        self.assertEqual(result['analysis']['final_risk'], .28)
        self.assertAlmostEqual(result['analysis']['confidence_score'], 72)
        self.assertEqual(result['analysis']['anomaly_types'], ['spoofing', 'multipath'])
        self.assertEqual(result['position']['speed'], 36)
        self.assertEqual(result['reference_position']['latitude'], raw['data']['estimated_latitude'])
        self.assertFalse(result['gnss']['fix_status'])
        self.assertEqual(result['gnss']['signal_quality_unit'], 'dB-Hz')
        raw['data']['fix_status'] = '3D'
        for risk, status in [(0, 'NOMINAL'), (.2, 'ATTENTION'), (.5, 'DEGRADED'), (.8, 'UNRELIABLE')]:
            prediction['final_risk'] = risk
            self.assertEqual(dashboard_message(raw, {}, prediction)['analysis']['status'], status)

    def test_nan_json_and_invalid_message(self):
        self.assertEqual(json.loads(dumps({'x': float('nan'), 'y': float('inf')})), {'x': None, 'y': None})
        raw = copy.deepcopy(self.messages[0])
        raw['data']['anomaly_label'] = 'normal'
        with self.assertRaises(ValueError):
            validate_raw(raw)

    def test_ack_after_publish_and_invalid_to_dlq(self):
        connection = Mock()
        channel = connection.channel.return_value
        classifier = self.classifier()
        consume(classifier, connection)
        callback = channel.basic_consume.call_args.kwargs['on_message_callback']
        method = Mock(delivery_tag=42)
        channel.reset_mock()
        callback(channel, method, None, dumps(self.messages[0]))
        self.assertEqual([c[0] for c in channel.method_calls], ['basic_publish', 'basic_ack'])
        channel.reset_mock()
        callback(channel, method, None, '{invalid json')
        channel.basic_reject.assert_called_once_with(42, requeue=False)
        channel.basic_ack.assert_not_called()
        channel.reset_mock()
        channel.basic_publish.side_effect = OSError('connection lost')
        with self.assertRaises(OSError):
            callback(channel, method, None, dumps(self.messages[1]))
        channel.basic_ack.assert_not_called()
        self.assertEqual(classifier.sessions['TRAIN_001']['delivered'], 0)
        channel.basic_publish.side_effect = None
        callback(channel, method, None, dumps(self.messages[1]))
        channel.basic_ack.assert_called_once_with(42)


if __name__ == '__main__':
    unittest.main()
