"""Independent sensor/crowd fusion and GNSS recovery hysteresis, per stream."""
import math
import os
from shared.contracts import has_fix, parse_timestamp
from shared.uncertainty import area, xy, latlon


def number(data, key, default):
    value = data.get(key)
    return value if type(value) in (float, int) and math.isfinite(value) else default


def position(data, prefix=''):
    lat, lon = data.get(prefix + 'latitude'), data.get(prefix + 'longitude')
    if (type(lat) in (int, float) and type(lon) in (int, float)
            and math.isfinite(lat) and math.isfinite(lon) and abs(lat) < 85 and abs(lon) <= 180):
        return dict(latitude=lat, longitude=lon)


class Localizer:
    def __init__(self, recovery_s=None, max_age_s=None, max_extent_m=None):
        self.recovery = float(os.getenv('GNSS_RECOVERY_S', '10')) if recovery_s is None else recovery_s
        self.max_age = float(os.getenv('SOURCE_MAX_AGE_S', '15')) if max_age_s is None else max_age_s
        self.max_extent = float(os.getenv('MAX_UNCERTAINTY_M', '2000')) if max_extent_m is None else max_extent_m
        if not all(math.isfinite(v) and v >= 0 for v in (self.recovery, self.max_age, self.max_extent)) or self.max_extent == 0:
            raise ValueError('Parametri localizzazione non validi')
        self.good_since = self.last_good = self.started = self.previous_time = None
        self.previous_gps = None
        self.trusted = False
        self.anchor_variance = 0

    def update(self, raw, analysis):
        d = raw['data']
        now = parse_timestamp(raw['timestamp']).timestamp()
        if self.started is None:
            self.started = now
        elapsed = max(0, now - (self.last_good if self.last_good is not None else self.started))
        sources = []
        quality = {}
        speed = max(0, number(d, 'estimated_speed', 0))
        heading = number(d, 'estimated_heading', 0)
        for name, prefix in [('sensors', 'estimated_'), ('users', 'crowd_')]:
            pos = position(d, prefix)
            stamp = d.get(prefix + 'timestamp')
            age = now - parse_timestamp(stamp).timestamp() if stamp else float('inf')
            valid = pos is not None and 0 <= age <= self.max_age
            if name == 'sensors':
                valid = valid and d.get('estimated_includes_crowd') is False
                # Exported posterior variance already includes accumulated position drift.
                base = max(1, self.anchor_variance, number(d, 'estimated_pos_var_m2', 100))
                along = base + number(d, 'estimated_speed_var_ms2', 1) * (elapsed + max(0, age)) ** 2
                lateral = base + (speed * elapsed * math.radians(math.sqrt(max(0, number(d, 'estimated_heading_var_deg2', 9))))) ** 2
            else:
                count = max(0, number(d, 'crowd_num_phones', 0))
                valid = valid and count > 0 and d.get('crowd_pos_std_m') is not None and d.get('crowd_accuracy_m') is not None
                # Common accuracy floor prevents an arbitrarily large crowd becoming perfect.
                base = max(4, number(d, 'crowd_accuracy_m', 50) ** 2) + number(d, 'crowd_pos_std_m', 100) ** 2 / max(1, count)
                along = lateral = base + (max(0, age) * max(speed, 1)) ** 2
            quality[name] = dict(usable=valid, age_s=age if math.isfinite(age) else None,
                                 variance_along_m2=along if valid else None,
                                 variance_lateral_m2=lateral if valid else None)
            if valid:
                sources.append(dict(name=name, position=pos, a=max(1, along), b=max(1, lateral)))
        gps = position(d)
        risk = analysis.get('final_risk')
        good = bool(gps and has_fix(d.get('fix_status')) and risk is not None and risk < .2 and not analysis.get('anomaly_types'))
        sigma = max(3, number(d, 'hdop', 10) * 5)
        if good and self.previous_gps and self.previous_time is not None:
            distance = math.hypot(*xy(gps['latitude'], gps['longitude'], list(self.previous_gps.values())))
            good = distance <= max(0, now - self.previous_time) * (max(speed, number(d, 'speed', 0)) + 10) + sigma * 3
        if good:
            for source in sources:
                dist = math.hypot(*xy(gps['latitude'], gps['longitude'], list(source['position'].values())))
                if dist > 3 * (sigma + math.sqrt(max(source['a'], source['b']))):
                    good = False
        if self.previous_time is not None and now - self.previous_time > self.max_age:
            self.good_since = None
            self.trusted = False
        if good:
            if self.good_since is None:
                self.good_since = now
            self.trusted = now - self.good_since >= self.recovery
        else:
            self.good_since = None
            self.trusted = False
        self.previous_gps, self.previous_time = gps, now
        if self.trusted:
            self.last_good = now
            self.anchor_variance = sigma ** 2
            elapsed = 0
            sources = [dict(name='gnss', position=gps, a=sigma ** 2, b=sigma ** 2)]
        elif gps and has_fix(d.get('fix_status')) and risk is not None and risk < .8:
            sources.append(dict(name='gnss_degraded', position=gps, a=(sigma * 4) ** 2, b=(sigma * 4) ** 2))
        disagreement = False
        for i, left in enumerate(sources):
            for right in sources[i + 1:]:
                distance = math.hypot(*xy(right['position']['latitude'], right['position']['longitude'], list(left['position'].values())))
                disagreement |= distance > 3 * math.sqrt(max(left['a'], left['b']) + max(right['a'], right['b']))
        fused = None
        shapes = []
        if sources:
            if disagreement:
                # Separate hypotheses, never an unsupported midpoint.
                hypotheses = sources
            else:
                origin = list(sources[0]['position'].values())
                weights = [1 / max(s['a'], s['b']) for s in sources]
                coords = [xy(s['position']['latitude'], s['position']['longitude'], origin) for s in sources]
                lat, lon = latlon(sum(w * p[0] for w, p in zip(weights, coords)) / sum(weights),
                                  sum(w * p[1] for w, p in zip(weights, coords)) / sum(weights), origin)
                fused = dict(latitude=lat, longitude=lon)
                # Conservative combination: no independence assumption about common errors.
                angle = math.radians(heading)
                residuals = [xy(s['position']['latitude'], s['position']['longitude'], [lat, lon]) for s in sources]
                between_a = sum(w * (x*math.sin(angle)+y*math.cos(angle))**2 for w,(x,y) in zip(weights,residuals))/sum(weights)
                between_b = sum(w * (x*math.cos(angle)-y*math.sin(angle))**2 for w,(x,y) in zip(weights,residuals))/sum(weights)
                hypotheses = [dict(position=fused, a=sum(w*s['a'] for w,s in zip(weights,sources))/sum(weights)+between_a,
                                   b=sum(w*s['b'] for w,s in zip(weights,sources))/sum(weights)+between_b)]
            shapes = [area(s['position'], 3 * math.sqrt(s['a']), 3 * math.sqrt(s['b']), heading) for s in hypotheses]
        indeterminate = not shapes or any(max(s['semi_along_m'], s['semi_lateral_m']) > self.max_extent for s in shapes)
        state = ('INDETERMINATE' if indeterminate else 'GNSS_RELIABLE' if self.trusted else
                 'GNSS_DEGRADED' if any(s['name'] == 'gnss_degraded' for s in sources) else 'ESTIMATED')
        return dict(state=state, position=fused, sources=[s['name'] for s in sources], quality=quality,
                    disagreement=disagreement, areas=shapes, last_reliable_age_s=elapsed if self.last_good is not None else None,
                    gnss_usable=state in ('GNSS_RELIABLE', 'GNSS_DEGRADED'),
                    clearance_evidence=self.trusted and not indeterminate and not disagreement,
                    control_room_required=indeterminate or disagreement,
                    message='Localizzazione non determinabile · verifica alla sala di controllo' if indeterminate else
                    'Fonti discordanti · ipotesi separate' if disagreement else
                    'GNSS affidabile' if self.trusted else 'GNSS degradato · confronto delle fonti' if state == 'GNSS_DEGRADED' else
                    'GNSS non disponibile · localizzazione stimata da ' + ' e '.join(s['name'].replace('sensors','sensori').replace('users','utenti') for s in sources))
