#!/usr/bin/env python3
"""Evaluate a configurable consistency baseline; never train a model."""
import argparse
import csv
import json
import math
from pathlib import Path
from tools.generation import EARTH_R


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('dataset', type=Path)
    p.add_argument('--split', choices=['train', 'validation', 'test'], default='test')
    p.add_argument('--position-threshold-m', type=float, default=25)
    p.add_argument('--hdop-threshold', type=float, default=5)
    p.add_argument('--min-satellites', type=int, default=4)
    args = p.parse_args()
    counts = dict(tp=0, fp=0, tn=0, fn=0)
    by_kind = {}
    manifest = json.loads((args.dataset / 'manifest.json').read_text())
    for item in manifest['trajectories']:
        if item['split'] != args.split:
            continue
        with (args.dataset / item['path']).open() as fh:
            for r in csv.DictReader(fh):
                lat, lon = float(r['latitude'] or 'nan'), float(r['longitude'] or 'nan')
                ref_lat, ref_lon = float(r['estimated_latitude']), float(r['estimated_longitude'])
                north = math.radians(lat - ref_lat) * EARTH_R
                east = math.radians(lon - ref_lon) * EARTH_R * math.cos(math.radians(ref_lat))
                predicted = (not math.isfinite(lat + lon) or r['fix_status'] == 'NO_FIX'
                    or math.hypot(east, north) > args.position_threshold_m
                    or float(r['hdop']) > args.hdop_threshold
                    or int(r['satellites_used']) < args.min_satellites)
                actual = r['anomaly_label'] != 'normal'
                counts[('tp' if actual else 'fp') if predicted else ('fn' if actual else 'tn')] += 1
                for kind in r['anomaly_label'].split('+'):
                    stat = by_kind.setdefault(kind, dict(samples=0, detected=0))
                    stat['samples'] += 1
                    stat['detected'] += int(predicted)
    def ratio(a, b):
        return a / b if b else None
    result = dict(split=args.split, thresholds=dict(position_m=args.position_threshold_m,
        hdop=args.hdop_threshold, min_satellites=args.min_satellites), **counts,
        precision=ratio(counts['tp'], counts['tp'] + counts['fp']),
        recall=ratio(counts['tp'], counts['tp'] + counts['fn']),
        f1=ratio(2 * counts['tp'], 2 * counts['tp'] + counts['fp'] + counts['fn']), by_kind=by_kind)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
