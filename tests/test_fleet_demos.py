import json
import unittest
from pathlib import Path

import joblib
import pandas as pd

from services.simulation.control import ReplayControl
from services.simulation.main import replay_batches
from services.simulation.scenario import network
from services.alerts.engine import AlertEngine
from shared.contracts import dashboard_message
from shared.inference import predict
from shared.localization import Localizer
from shared.preprocessing import RiskProcessor

ROOT = Path(__file__).resolve().parents[1]


class FleetDemoTests(unittest.TestCase):
    def test_replay_inference_and_network_alerts(self):
        processor = RiskProcessor.load(ROOT/'models/preprocessing.joblib')
        models = joblib.load(ROOT/'models/random_forest_models.joblib')
        control = ReplayControl(ROOT,ROOT/'gnss_tracks/demo_switch.csv')
        catalog = {row['id'] for row in control.catalog()}
        self.assertFalse(any(p.endswith('.vehicle.csv') for p in catalog))
        engine = AlertEngine(network())
        for size,count in ((3,6),(4,8)):
            name = f'demo_grid_{count}_trains'
            path = ROOT/'gnss_tracks'/name/f'{name}.csv'
            self.assertIn(path.relative_to(ROOT).as_posix(),catalog)
            geometry_id = path.with_suffix('.geometry.csv').relative_to(ROOT).as_posix()
            spec = json.loads(path.with_suffix('.scenario.json').read_text())
            net = json.loads((ROOT/'infrastructure/network'/f'{name}.network.json').read_text())
            engine.register_network(net)
            predictions = {}
            indicators = {}
            localizers = {}
            for vehicle in spec['vehicles']:
                frame = pd.read_csv(path.parent/vehicle['csv'])
                geometry = pd.read_csv(path.parent/vehicle['geometry'])
                features = processor.transform(frame,geometry)
                predictions[vehicle['id']] = predict(models,features)
                indicators[vehicle['id']] = features
                localizers[vehicle['id']] = Localizer()
            saw_no_fix = saw_ml_error = saw_alert = False
            maximum_recipients = 0
            for batch in replay_batches(path,'TRAIN_001',geometry_id,name,ROOT):
                self.assertEqual(len(batch),count)
                self.assertEqual(len({r['timestamp'] for r in batch}),1)
                for raw in batch:
                    vid,i = raw['vehicle_id'],raw['sequence']
                    result = dashboard_message(raw,indicators[vid].iloc[i] if i>=2 else None,
                                               predictions[vid].iloc[i] if i>=2 else None)
                    result['localization'] = localizers[vid].update(raw,result['analysis'])
                    result['network_id'],result['traffic'] = raw['network_id'],raw['traffic']
                    snapshot = engine.update(result)
                    saw_no_fix |= result['analysis']['status']=='UNRELIABLE'
                    saw_ml_error |= bool(result['analysis']['anomaly_types'])
                    saw_alert |= bool(snapshot['alerts'])
                    maximum_recipients = max([maximum_recipients]+[len(a['recipients']) for a in snapshot['alerts']])
            self.assertEqual(len(snapshot['vehicles']),count)
            self.assertEqual(sum(s['kind']=='switch' for s in snapshot['network']),size**2)
            self.assertTrue(saw_no_fix)
            self.assertTrue(saw_ml_error)
            self.assertTrue(saw_alert)
            self.assertGreater(maximum_recipients,1)
            print(name,'ML anomalies, loss of fix, fleet and multi-recipient alerts verified')
        # A previous session retains its own network after another demo runs.
        self.assertEqual(engine.snapshot('demo_grid_6_trains')['network_id'],'demo_grid_6_trains')
        self.assertEqual(len(engine.snapshot('demo_grid_6_trains')['vehicles']),6)


if __name__ == '__main__':
    unittest.main()
