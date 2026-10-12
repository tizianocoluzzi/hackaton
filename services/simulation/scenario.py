"""Synchronized railway demo: shared clock, switch alternatives and overpass."""
from copy import deepcopy
from shared.contracts import parse_timestamp
from shared.uncertainty import latlon

NETWORK_ID = 'demo-switch-v1'
ORIGIN = (45, 9)


def network():
    def segment(sid, points, connections, kind='track', level=0):
        return dict(id=sid, coordinates=[latlon(x, y, ORIGIN) for x, y in points],
                    connections=connections, kind=kind, level=level, width_m=2)
    return dict(id=NETWORK_ID, expected_vehicles=['TRAIN_001', 'TRAIN_002', 'TRAIN_003'], segments=[
        segment('approach', [(0,-1000),(0,-20)], ['switch']),
        segment('switch', [(0,-20),(0,20)], ['approach','branch-a','branch-b'], 'switch'),
        segment('branch-a', [(0,20),(0,1200)], ['switch']),
        segment('branch-b', [(0,20),(70,160),(70,1200)], ['switch']),
        segment('overpass', [(-300,0),(300,0)], [], 'bridge', 1)])


def fleet(primary):
    """Each tick includes every vehicle, using the primary row timestamp."""
    now = parse_timestamp(primary['timestamp']).timestamp()
    tick = primary['sequence']
    start = now - tick
    primary['network_id'] = NETWORK_ID
    primary['traffic'] = dict(level=0, route=['approach','switch','branch-a'], direction=1,
                             transits=[dict(zone_id='switch', enter=start+25, exit=start+40)])
    yield primary
    for vid, east, north, enter in [('TRAIN_002',70,800-3*tick,180), ('TRAIN_003',0,1100,900)]:
        other = deepcopy(primary)
        other['vehicle_id'] = vid
        lat, lon = latlon(east,north,ORIGIN)
        d = other['data']
        for prefix in ('','estimated_','crowd_'):
            d[prefix+'latitude'], d[prefix+'longitude'] = lat,lon
        d.update(speed=3, heading=180, estimated_speed=3, estimated_heading=180,
                 estimated_pos_var_m2=4, estimated_speed_var_ms2=.01, estimated_heading_var_deg2=.1,
                 fix_status='3D', hdop=.7, satellites_used=14, crowd_num_phones=12,
                 crowd_pos_std_m=3, crowd_accuracy_m=3)
        other['traffic'] = dict(level=0, direction=-1, route=['branch-b','switch','approach'],
                               transits=[dict(zone_id=z,enter=start+enter,exit=start+enter+30)
                                         for z in ('branch-b','switch')])
        yield other
