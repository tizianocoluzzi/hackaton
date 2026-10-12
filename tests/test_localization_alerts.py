import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from shared.localization import Localizer
from shared.uncertainty import area, latlon, intersects
from services.alerts.engine import AlertEngine
from services.alerts.main import Store
from services.simulation.scenario import network, ORIGIN


def raw(t=0, **changes):
    stamp = (datetime(2025,1,1,tzinfo=timezone.utc)+timedelta(seconds=t)).isoformat()
    data = dict(latitude=45., longitude=9., fix_status='3D', hdop=1, speed=5,
                estimated_latitude=45., estimated_longitude=9., estimated_speed=5, estimated_heading=0,
                estimated_pos_var_m2=4, estimated_speed_var_ms2=1, estimated_heading_var_deg2=9,
                estimated_timestamp=stamp, estimated_includes_crowd=False,
                crowd_latitude=45., crowd_longitude=9., crowd_timestamp=stamp,
                crowd_num_phones=10, crowd_pos_std_m=6, crowd_accuracy_m=5)
    data.update(changes)
    return dict(timestamp=stamp, data=data)


GOOD = dict(final_risk=0.01, anomaly_types=[])
BAD = dict(final_risk=.95, anomaly_types=['spoofing'])


def vehicle(t=0, y=0, vid='TRAIN_001', session='one', reliable=False, level=0, radius=10):
    lat,lon = latlon(0,y,ORIGIN)
    pos = dict(latitude=lat,longitude=lon)
    return dict(session_id=session, vehicle_id=vid, sequence=t, timestamp=raw(t)['timestamp'],
                network_id='demo-switch-v1', localization=dict(position=pos, areas=[area(pos,radius,radius)],
                    clearance_evidence=reliable, control_room_required=False),
                traffic=dict(level=level, direction=1, route=['approach','switch','branch-a'],transits=[]))


class LocalizationTests(unittest.TestCase):
    def test_loss_recovery_and_anomaly_with_fix(self):
        loc=Localizer(recovery_s=2)
        self.assertNotEqual(loc.update(raw(),GOOD)['state'],'GNSS_RELIABLE')
        self.assertEqual(loc.update(raw(2),GOOD)['state'],'GNSS_RELIABLE')
        self.assertEqual(loc.update(raw(3,fix_status='NO_FIX'),BAD)['state'],'ESTIMATED')
        self.assertEqual(loc.update(raw(4),BAD)['state'],'ESTIMATED')
        self.assertNotEqual(loc.update(raw(5),GOOD)['state'],'GNSS_RELIABLE')
        self.assertEqual(loc.update(raw(7),GOOD)['state'],'GNSS_RELIABLE')

    def test_stale_missing_correlated_and_unknown_quality(self):
        loc=Localizer()
        stale=raw(30,fix_status='NO_FIX',crowd_timestamp=raw()['timestamp'])
        self.assertEqual(loc.update(stale,BAD)['sources'],['sensors'])
        stale['data']['estimated_includes_crowd']=True
        self.assertEqual(loc.update(stale,BAD)['state'],'INDETERMINATE')
        self.assertEqual(Localizer().update(raw(fix_status='NO_FIX',estimated_timestamp=None,crowd_accuracy_m=None),BAD)['state'],'INDETERMINATE')

    def test_disagreement_no_midpoint_and_metric_fusion(self):
        result=Localizer().update(raw(fix_status='NO_FIX',crowd_longitude=9.1),BAD)
        self.assertTrue(result['disagreement']); self.assertIsNone(result['position'])
        self.assertEqual(len(result['areas']),2)
        result=Localizer().update(raw(fix_status='NO_FIX',crowd_longitude=9.0001),BAD)
        self.assertGreater(result['position']['longitude'],9)
        self.assertLess(result['position']['longitude'],9.00005)

    def test_drift_ellipse_and_excessive_extent(self):
        loc=Localizer(max_extent_m=200)
        a=loc.update(raw(fix_status='NO_FIX',crowd_num_phones=0),BAD)
        b=loc.update(raw(30,fix_status='NO_FIX',crowd_num_phones=0),BAD)
        self.assertGreater(b['areas'][0]['semi_along_m'],a['areas'][0]['semi_along_m'])
        self.assertEqual(b['areas'][0]['kind'],'ellipse')
        self.assertEqual(loc.update(raw(100,fix_status='NO_FIX',crowd_num_phones=0),BAD)['state'],'INDETERMINATE')

    def test_segment_interior_and_bridge(self):
        pos=dict(latitude=45,longitude=9)
        shape=area(pos,10,10)
        self.assertTrue(intersects(shape,[latlon(-100,0,ORIGIN),latlon(100,0,ORIGIN)]))
        engine=AlertEngine(network())
        snap=engine.update(vehicle())
        self.assertNotIn('overpass',{z['id'] for z in snap['zones']})
        self.assertIn('switch',{z['id'] for z in snap['zones']})


class AlertTests(unittest.TestCase):
    def test_two_branches_persistence_stable_clearance(self):
        engine=AlertEngine(network(),clearance_s=10)
        snap=engine.update(vehicle(y=100,radius=100))
        self.assertTrue({'branch-a','branch-b','switch'} <= {z['id'] for z in snap['zones']})
        message=vehicle(1,y=1000)
        message['localization']['areas']=[]
        snap=engine.update(message)
        self.assertIn('switch',{z['id'] for z in snap['zones']})
        for t in (2,7):
            snap=engine.update(vehicle(t,y=1000,reliable=True))
            self.assertIn('switch',{z['id'] for z in snap['zones']})
        snap=engine.update(vehicle(12,y=1000,reliable=True))
        self.assertNotIn('switch',{z['id'] for z in snap['zones']})
        self.assertTrue(any(a['zone_id']=='switch' and a['risk_state']=='RESOLVED' for a in snap['alerts']))

    def test_temporal_overlap_duplicate_receipt_retry_session(self):
        engine=AlertEngine(network())
        engine.update(vehicle())
        arrival=vehicle(0,y=800,vid='TRAIN_002')
        now=datetime.fromisoformat(arrival['timestamp']).timestamp()
        arrival['traffic']['transits']=[dict(zone_id='switch',enter=now+100,exit=now+120)]
        engine.update(arrival)
        late=vehicle(0,y=1000,vid='TRAIN_003')
        late['traffic']['transits']=[dict(zone_id='switch',enter=now+800,exit=now+820)]
        snap=engine.update(late)
        alert=next(a for a in snap['alerts'] if a['vehicle_id']=='TRAIN_001' and a['zone_id']=='switch')
        self.assertIn('TRAIN_002',alert['recipients']); self.assertNotIn('TRAIN_003',alert['recipients'])
        aid=alert['id']
        count=len(snap['alerts']); engine.update(late)
        self.assertEqual(len(engine.snapshot('one')['alerts']),count)
        engine.dispatch('one',lambda *args:False)
        self.assertEqual(alert['notifications']['TRAIN_002']['state'],'SENDING')
        engine.update(vehicle(5))
        engine.dispatch('one',lambda *args:True)
        self.assertEqual(alert['notifications']['TRAIN_002']['state'],'DELIVERED')
        self.assertEqual(alert['risk_state'],'DETECTED')
        engine.receipt('one',aid,'TRAIN_002',acknowledged=True)
        self.assertEqual(alert['notifications']['TRAIN_002']['state'],'ACKNOWLEDGED')
        self.assertFalse(engine.snapshot('two')['alerts'])

    def test_gap_does_not_count_as_clearance_and_nearby_is_not_occupied(self):
        engine=AlertEngine(network(),clearance_s=10)
        engine.update(vehicle())
        engine.update(vehicle(1,y=900,reliable=True))
        snap=engine.update(vehicle(30,y=900,reliable=True))
        self.assertIn('switch',{z['id'] for z in snap['zones']})
        near=vehicle(0,session='near',radius=1)
        pos=dict(zip(('latitude','longitude'),latlon(25,0,ORIGIN)))
        near['localization'].update(position=pos,areas=[area(pos,1,1)])
        snap=engine.update(near)
        self.assertFalse(snap['occupations'])
        self.assertIn('switch',snap['vehicles'][0]['nearby_zones'])

    def test_normal_occupancy_without_conflict_does_not_alert(self):
        engine=AlertEngine(network())
        self.assertFalse(engine.update(vehicle(reliable=True))['alerts'])

    def test_outbox_survives_restart_and_failed_publish(self):
        class Channel:
            def basic_publish(self,**kwargs):
                raise OSError('disconnected')
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'state.sqlite')
            store=Store(path,AlertEngine(network()))
            store.accept(vehicle(),lambda *args:False)
            with self.assertRaises(OSError):store.flush(Channel())
            store.db.close()
            restored=Store(path,AlertEngine(network()))
            self.assertTrue(restored.engine.snapshot('one')['alerts'])
            self.assertEqual(restored.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
            restored.accept(vehicle(),lambda *args:False)
            self.assertEqual(len(restored.engine.snapshot('one')['alerts']),1)
            restored.db.close()

if __name__=='__main__':unittest.main()
