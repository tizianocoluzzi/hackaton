"""Deterministic railway fixture; python -m tools.generate_alert_demo."""
import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from services.simulation.scenario import network, ORIGIN
from shared.uncertainty import latlon


def main():
    root = Path(__file__).resolve().parents[1]
    (root/'infrastructure/network/demo.json').write_text(json.dumps(network(), indent=2)+'\n')
    rows=[]
    for t in range(181):
        lost=25 <= t < 105
        lat,lon=latlon(0,-150+5*t,ORIGIN)
        rows.append(dict(timestamp=(datetime(2025,1,1,8,tzinfo=timezone.utc)+timedelta(seconds=t)).isoformat(),
            latitude=None if lost else lat,longitude=None if lost else lon,speed=5,heading=0,
            satellites_used=0 if lost else 14,hdop=20 if lost else .7,fix_status='NO_FIX' if lost else '3D',
            estimated_latitude=lat,estimated_longitude=lon,estimated_speed=5,estimated_heading=0,
            estimated_pos_var_m2=4+max(0,t-25)*3 if lost else 4,
            estimated_speed_var_ms2=.8 if lost else .01,estimated_heading_var_deg2=16 if lost else .1,
            crowd_latitude=None if lost else lat,crowd_longitude=None if lost else lon,
            crowd_num_phones=0 if lost else 10,crowd_pos_std_m=4,crowd_accuracy_m=3))
    with (root/'gnss_tracks/demo_switch.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    with (root/'gnss_tracks/demo_switch.geometry.csv').open('w') as f:
        writer=csv.writer(f);writer.writerow(['latitude','longitude','distance_m'])
        writer.writerows(latlon(0,y,ORIGIN)+[y+1000] for y in range(-1000,1201,10))

if __name__=='__main__':
    main()
