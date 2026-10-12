"""Generate deterministic fleet CSVs, descriptors and matching alert networks."""
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shared.uncertainty import latlon

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = (45, 9)


def write_csv(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate(name, size):
    folder = ROOT / 'gnss_tracks' / name
    folder.mkdir(parents=True, exist_ok=True)
    axes = [i * 240 for i in range(size)]
    end = axes[-1] + 300
    duration = math.ceil((end + 200) / 5)
    segments = []
    for i, x in enumerate(axes):
        for j, y in enumerate(axes):
            segments.append(dict(id=f'cross-{i+1}-{j+1}', coordinates=[latlon(x,y-15,ORIGIN), latlon(x,y+15,ORIGIN)],
                                 connections=[], kind='switch', level=0, width_m=15))
    for orientation in ('v', 'h'):
        for i, fixed in enumerate(axes):
            for k in range(size+1):
                lo = -300 if k == 0 else axes[k-1]+15
                hi = end if k == size else axes[k]-15
                points = [(fixed,lo),(fixed,hi)] if orientation == 'v' else [(lo,fixed),(hi,fixed)]
                neighbors = [f'cross-{i+1}-{j+1}' if orientation == 'v' else f'cross-{j+1}-{i+1}'
                             for j in (k-1,k) if 0 <= j < size]
                sid = f'{orientation}-{i+1}-{k}'
                segments.append(dict(id=sid, coordinates=[latlon(*p,ORIGIN) for p in points], connections=neighbors,
                                     kind='track',level=0,width_m=2))
                for segment in segments:
                    if segment['id'] in neighbors:
                        segment['connections'].append(sid)
    vehicles = []
    for index in range(size*2):
        orientation = 'v' if index < size else 'h'
        lane = index % size
        reverse = lane % 2 == 1
        direction = -1 if reverse else 1
        initial = end-100 if reverse else -200
        speed = 5
        heading = (180 if reverse else 0) if orientation == 'v' else (270 if reverse else 90)
        rows = []
        events = [(30,70,'tunnel'),(80,110,'spoofing'),(120,145,'multipath'),(150,175,'low_satellites')]
        for t in range(duration+1):
            progress = initial + direction*speed*t
            east,north = (axes[lane],progress) if orientation == 'v' else (progress,axes[lane])
            label = next((kind for a,b,kind in events if a <= t-index*3 < b), 'normal')
            offset = 180 if label == 'spoofing' else 70*math.sin(t*2) if label == 'multipath' else 0
            lat,lon = latlon(east,north,ORIGIN)
            glat,glon = latlon(east+offset,north,ORIGIN)
            lost = label == 'tunnel'
            rows.append(dict(timestamp=(datetime(2025,1,1,8,tzinfo=timezone.utc)+timedelta(seconds=t)).isoformat(),
                latitude=None if lost else glat,longitude=None if lost else glon,speed=speed,heading=heading,
                satellites_used=0 if lost else 3 if label=='low_satellites' else 14,
                hdop=20 if lost else 8 if label=='low_satellites' else .7,fix_status='NO_FIX' if lost else '3D',
                signal_quality=10 if lost else 42,anomaly_label=label,
                estimated_latitude=lat,estimated_longitude=lon,estimated_speed=speed,estimated_heading=heading,
                estimated_pos_var_m2=4+(t-index*3-30)*3 if lost else 4,
                estimated_speed_var_ms2=.8 if lost else .01,estimated_heading_var_deg2=16 if lost else .1,
                crowd_latitude=None if lost else lat,crowd_longitude=None if lost else lon,
                crowd_num_phones=0 if lost else 12,crowd_pos_std_m=4,crowd_accuracy_m=3))
        csv_name = f'train_{index+1:03}.vehicle.csv' if index else f'{name}.csv'
        geometry_name = f'train_{index+1:03}.geometry.csv' if index else f'{name}.geometry.csv'
        write_csv(folder/csv_name, rows)
        geometry = []
        for distance in range(-1200, end+1201, 10):
            x,y = (axes[lane],distance) if orientation=='v' else (distance,axes[lane])
            lat,lon = latlon(x,y,ORIGIN)
            geometry.append(dict(latitude=lat,longitude=lon,distance_m=distance+1200))
        write_csv(folder/geometry_name, geometry)
        route = []
        transits = []
        for k in range(size+1):
            route.append(f'{orientation}-{lane+1}-{k}')
            if k<size:
                sid = f'cross-{lane+1}-{k+1}' if orientation=='v' else f'cross-{k+1}-{lane+1}'
                route.append(sid)
                arrival = (axes[k]-initial)/(direction*speed)
                transits.append(dict(zone_id=sid,enter=arrival-4,exit=arrival+4))
        if reverse:
            route.reverse()
        vehicles.append(dict(id=f'TRAIN_{index+1:03}',csv=csv_name,geometry=geometry_name,route=route,
                             direction=direction,level=0,transits=transits))
    network = dict(id=name,expected_vehicles=[v['id'] for v in vehicles],segments=segments)
    (ROOT/'infrastructure/network'/f'{name}.network.json').write_text(json.dumps(network,indent=2)+'\n')
    (folder/f'{name}.scenario.json').write_text(json.dumps(dict(network_id=name,vehicles=vehicles),indent=2)+'\n')
    print(name, len(vehicles), 'trains',size**2,'crossings',duration+1,'ticks')


if __name__ == '__main__':
    generate('demo_grid_6_trains',3)
    generate('demo_grid_8_trains',4)
