"""Track-only scenario: geometry, replay integration and virtual delivery."""
import json
import unittest
from pathlib import Path
import joblib
import pandas as pd
from services.alerts.engine import AlertEngine
from services.simulation.main import replay_batches
from shared.contracts import dashboard_message
from shared.inference import predict
from shared.localization import Localizer
from shared.preprocessing import RiskProcessor

ROOT = Path(__file__).resolve().parents[1]

class TrackNetworkTests(unittest.TestCase):
    def test_existing_track_anomaly_hits_junction_and_parallel(self):
        self.check_track('track_000_mixed.csv', 0)

    def test_short_presentation_replay(self):
        self.check_track('scenario_bivio.csv', 280)

    def check_track(self, filename, offset):
        path = ROOT/'gnss_tracks'/filename
        net = json.loads((ROOT/'infrastructure/network/track_mixed_junction.network.json').read_text())
        frame = pd.read_csv(path)
        geometry = path.with_suffix('.geometry.csv')
        features = RiskProcessor.load(ROOT/'models/preprocessing.joblib').transform(frame, pd.read_csv(geometry))
        predictions = predict(joblib.load(ROOT/'models/random_forest_models.joblib'), features)
        engine = AlertEngine(net)
        localizer = Localizer()
        seen = set()
        first = {}
        for batch in replay_batches(path,'TRAIN_001',str(geometry.relative_to(ROOT)),'junction-test',ROOT):
            self.assertEqual(len(batch),1)  # No other train positions are generated.
            raw = batch[0]
            i = raw['sequence']
            msg = dashboard_message(raw, features.iloc[i] if i>=2 else None, predictions.iloc[i] if i>=2 else None)
            msg['localization'] = localizer.update(raw,msg['analysis'])
            msg['network_id'],msg['traffic'] = raw['network_id'],raw['traffic']
            snapshot = engine.update(msg)
            engine.dispatch('junction-test',lambda *args: True)
            zones = {z['id'] for z in snapshot['zones']}
            self.assertNotIn('CAVALCAVIA',zones)
            self.assertNotIn('BINARIO_ESTERNO',zones)
            if 300 <= i + offset < 420:
                seen |= zones
                for z in zones: first.setdefault(z,i)
            if i + offset < 280:
                self.assertFalse(zones)
        self.assertTrue({'BIVIO','DIRAMAZIONE','PARALLELO','ATTRAVERSAMENTO'} <= seen, seen)
        alerts = engine.snapshot('junction-test')['alerts']
        self.assertTrue(any('TRAIN_P3' in a['recipients'] for a in alerts))
        self.assertTrue(any('TRAIN_B2' in a['recipients'] for a in alerts))
        self.assertTrue(all(n['simulated'] for a in alerts for n in a['notifications'].values()))
        self.assertTrue(all(a['risk_state']=='RESOLVED' for a in alerts))
        self.assertEqual(len(snapshot['vehicles']),1)
        print('First affected samples:',first)

if __name__ == '__main__': unittest.main()
