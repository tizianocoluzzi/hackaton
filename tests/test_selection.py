import copy
from pathlib import Path
import unittest

from services.simulation.control import ReplayControl
from services.simulation.main import replay_messages
from shared.contracts import dashboard_message

ROOT = Path(__file__).resolve().parents[1]
INITIAL = ROOT / 'gnss_tracks/track_000_mixed.csv'


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.control = ReplayControl(ROOT, INITIAL)

    def test_catalog_only_replay_files_and_full_geometry(self):
        catalog = self.control.catalog()
        self.assertGreater(len(catalog), 1)
        self.assertIn('gnss_tracks/track_000_mixed.csv', [item['id'] for item in catalog])
        self.assertTrue(all(not item['id'].endswith(('.truth.csv', '.geometry.csv')) for item in catalog))
        geometry = self.control.geometry('gnss_tracks/track_000_mixed.csv')
        self.assertGreater(len(geometry['coordinates']), 3)
        self.assertEqual(geometry['source'], 'track_geometry')
        with self.assertRaises(ValueError):
            self.control.select('../../etc/passwd')

    def test_change_and_restart_generate_new_session(self):
        first = self.control.snapshot()
        second = self.control.select('dataset/test/route_0006/scenario_01.csv')
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertTrue(self.control.changed(first['session_id']))
        self.assertEqual(second['geometry_id'], 'dataset/test/route_0006/geometry.csv')
        # La fine del vecchio replay non può sovrascrivere la selezione nuova.
        self.control.repeat(first['session_id'])
        self.assertEqual(self.control.snapshot(), second)
        restarted = self.control.select(second['file_id'])
        self.assertNotEqual(second['session_id'], restarted['session_id'])
        message = next(replay_messages(ROOT / restarted['file_id'], 'TRAIN',
                                       restarted['geometry_id'], restarted['session_id']))
        self.assertEqual(message['sequence'], 0)
        self.assertEqual(message['session_id'], restarted['session_id'])

    def test_crowd_is_independent_of_gps_fix_and_reference(self):
        raw = next(replay_messages(INITIAL, 'TRAIN', 'gnss_tracks/track_000_mixed.geometry.csv'))
        raw['source_file'] = 'gnss_tracks/track_000_mixed.csv'
        raw = copy.deepcopy(raw)
        raw['data'].update(fix_status='NO_FIX', latitude=None, longitude=None)
        result = dashboard_message(raw)
        self.assertEqual(result['source_file'], raw['source_file'])
        self.assertEqual(result['crowd_position']['latitude'], raw['data']['crowd_latitude'])
        self.assertEqual(result['crowd_position']['longitude'], raw['data']['crowd_longitude'])
        self.assertEqual(result['crowd_position']['num_phones'], raw['data']['crowd_num_phones'])
        self.assertNotEqual(result['crowd_position']['latitude'], result['reference_position']['latitude'])
        self.assertFalse(result['gnss']['fix_status'])


if __name__ == '__main__':
    unittest.main()
