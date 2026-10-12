"""Add reproducible track-only conflict fixtures around the existing spoofing episode."""
import csv
import json
import math
from pathlib import Path
from shared.uncertainty import latlon, xy

ROOT = Path(__file__).resolve().parents[1]


def generate(root=ROOT):
    path = root / 'gnss_tracks/track_000_mixed.csv'
    rows = list(csv.DictReader(path.open()))
    geometry = list(csv.DictReader(path.with_suffix('.geometry.csv').open()))
    bad = [i for i, row in enumerate(rows) if row['anomaly_label'] != 'normal']
    tick = bad[len(bad)//4]
    row = rows[tick]
    estimate = (float(row['estimated_latitude']), float(row['estimated_longitude']))
    nearest = min(geometry, key=lambda p: math.hypot(*xy(float(p['latitude']), float(p['longitude']), estimate)))
    origin = (float(nearest['latitude']), float(nearest['longitude']))
    angle = math.radians(float(row['estimated_heading']))
    def point(along, lateral):
        return latlon(along*math.sin(angle)+lateral*math.cos(angle),
                      along*math.cos(angle)-lateral*math.sin(angle), origin)
    segments = []
    def segment(sid, points, recipients=(), level=0, connections=()):
        segments.append(dict(id=sid, coordinates=[point(*p) for p in points], connections=list(connections),
                             level=level, width_m=2, kind='bridge' if level else 'track',
                             simulated_recipients=list(recipients)))
    segment('BIVIO', [(-30,0),(30,0)], ['TRAIN_B2'], connections=['DIRAMAZIONE'])
    segment('DIRAMAZIONE', [(0,0),(80,35),(220,140)], ['TRAIN_B2'], connections=['BIVIO'])
    segment('PARALLELO', [(-160,25),(200,25)], ['TRAIN_P3'])
    segment('ATTRAVERSAMENTO', [(0,-160),(0,160)], ['TRAIN_C4'])
    segment('BINARIO_ESTERNO', [(-160,450),(200,450)], ['TRAIN_E5'])
    segment('CAVALCAVIA', [(0,-180),(0,180)], ['TRAIN_S6'], level=1)
    net = dict(id='track-mixed-junction-v1', expected_vehicles=['TRAIN_001'], simulated=True,
               scenario_anchor=dict(sequence=tick, timestamp=row['timestamp'], latitude=origin[0], longitude=origin[1]), segments=segments)
    target = root/'infrastructure/network/track_mixed_junction.network.json'
    target.write_text(json.dumps(net, indent=2)+'\n')
    spec = dict(network_id=net['id'], vehicles=[dict(id='TRAIN_001', csv=path.name,
                geometry=path.with_suffix('.geometry.csv').name, level=0, direction=1,
                route=['BIVIO'], transits=[])])
    path.with_suffix('.scenario.json').write_text(json.dumps(spec, indent=2)+'\n')
    # A short presentation replay preserves the original measurements and timestamps.
    short = path.with_name('scenario_bivio.csv')
    with short.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows[max(0,bad[0]-20):min(len(rows),bad[-1]+62)])
    short.with_suffix('.geometry.csv').write_text(path.with_suffix('.geometry.csv').read_text())
    short_spec = json.loads(json.dumps(spec))
    short_spec['vehicles'][0].update(csv=short.name, geometry=short.with_suffix('.geometry.csv').name)
    short.with_suffix('.scenario.json').write_text(json.dumps(short_spec, indent=2)+'\n')
    return net


if __name__ == '__main__':
    result = generate()
    print(json.dumps(result['scenario_anchor']))
