#!/usr/bin/env python3
"""Generate grouped railway datasets with exhaustive isolated/overlapping events."""
import argparse
import dataclasses
import itertools
import json
from pathlib import Path
import numpy as np
from tools.generation import (TrackConfig, TRACK_TYPES, ANOMALY_TYPES, AnomalySpec,
                  generate_track, write_csv, write_geometry, validate_config)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', default='dataset')
    p.add_argument('--routes-per-split', type=int, default=3)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--config', help='JSON TrackConfig overrides')
    p.add_argument('--event-params', help='JSON object mapping anomaly kinds to parameter overrides')
    p.add_argument('--event-duration-s', type=float, default=30)
    p.add_argument('--margin-s', type=float, default=10)
    p.add_argument('--overlap-fraction', type=float, default=0.5)
    args = p.parse_args()
    if args.routes_per_split < 1 or args.event_duration_s <= 0 or args.margin_s < 0 or not 0 < args.overlap_fraction <= 1:
        p.error('invalid count, event duration, margin or overlap fraction')
    overrides = json.loads(Path(args.config).read_text()) if args.config else {}
    if 'start_time' in overrides:
        from datetime import datetime
        overrides['start_time'] = datetime.fromisoformat(overrides['start_time'].replace('Z', '+00:00'))
    event_params = json.loads(Path(args.event_params).read_text()) if args.event_params else {}
    if set(event_params) - set(ANOMALY_TYPES):
        p.error("unknown anomaly kind in event parameters")
    base = dataclasses.replace(TrackConfig(), **overrides)
    if base.n_random_anomalies:
        p.error('n_random_anomalies must be zero: scenarios control event coverage')
    validate_config(base)
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    groups = [()] + [(k,) for k in ANOMALY_TYPES] + list(itertools.combinations(ANOMALY_TYPES, 2))
    children = np.random.SeedSequence(args.seed).spawn(3 * args.routes_per_split)
    for index, child in enumerate(children):
        split = ('train', 'validation', 'test')[index // args.routes_per_split]
        route_id = f'route_{index:04d}'
        seed = int(child.generate_state(1, dtype=np.uint64)[0])
        planner = np.random.default_rng(seed)
        cfg = dataclasses.replace(base, track_type=TRACK_TYPES[index % len(TRACK_TYPES)])
        nominal, _ = generate_track(cfg, rng=np.random.default_rng(seed))
        end = (len(nominal) - 1) / cfg.rate_hz
        shift = args.event_duration_s * (1 - args.overlap_fraction)
        if end < 2 * args.margin_s + args.event_duration_s + shift:
            raise ValueError('route too short for requested events/margins')
        folder = root / split / route_id
        folder.mkdir(parents=True, exist_ok=True)
        write_geometry(cfg, folder / 'geometry.csv', seed)
        for scenario, kinds in enumerate(groups):
            start = float(planner.uniform(args.margin_s, end - args.margin_s - args.event_duration_s - shift))
            events = [AnomalySpec(k, start + j * shift, args.event_duration_s, dict(event_params.get(k, {}))) for j, k in enumerate(kinds)]
            rows, specs = generate_track(cfg, events, np.random.default_rng(seed))
            # Keep the alternative estimate identical across variants of a route:
            # event generation may otherwise change downstream RNG consumption.
            for r, reference in zip(rows, nominal):
                for key in reference:
                    if key.startswith('estimated_'):
                        r[key] = reference[key]
            expected = set(kinds)
            observed = set('+'.join(r['anomaly_label'] for r in rows).split('+'))
            if not expected <= observed or (len(kinds) == 2 and not any('+' in r['anomaly_label'] for r in rows)):
                raise ValueError('sampling rate/duration does not realize requested events')
            path = folder / f'scenario_{scenario:02d}.csv'
            write_csv(rows, path, with_truth=False, imu=cfg.enable_imu, crowd=cfg.enable_crowd)
            # Ground truth stays outside input features.
            with (folder / f'scenario_{scenario:02d}.truth.csv').open('w') as fh:
                fh.write('timestamp,truth_latitude,truth_longitude,truth_speed\n')
                for r in rows:
                    fh.write(f"{r['timestamp']},{r['_truth_lat']},{r['_truth_lon']},{r['_truth_speed']}\n")
            manifest.append(dict(split=split, route_id=route_id, path=str(path.relative_to(root)),
                                 seed=seed, samples=len(rows), events=[dataclasses.asdict(a) for a in specs],
                                 config=dataclasses.asdict(cfg)))
    (root / 'manifest.json').write_text(json.dumps(dict(generator_seed=args.seed,
        event_duration_s=args.event_duration_s, margin_s=args.margin_s,
        overlap_fraction=args.overlap_fraction, trajectories=manifest), indent=2, default=str))
    print(f'{len(manifest)} trajectories written to {root}')


if __name__ == '__main__':
    main()
