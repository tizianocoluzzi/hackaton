"""Indicatori GNSS condivisi tra batch offline e classificazione live."""
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

EARTH_R = 6378137.
DEFAULT_CFG = {
    'window_s': 10.0,
    'smooth_s': 3.0,
    'baseline_s': 15.0,
    'min_motion_ms': 2.0,
    'sat_good': 12.0,
    'sat_critical': 4.0,
    'acceleration': (1.2, 3.0),
    'jerk': (0.5, 2.0),
    'lateral': (1.2, 3.0),
    'prediction': (5.0, 30.0),
    'crowd_rate': (0.3, 3.0),
    'crowd_offset': (10.0, 60.0),
    'crowd_min_phones': 3,
    'map_distance': (5.0, 30.0),
    'map_progress': (10.0, 60.0),
    'state_d2': (5.99, 25.0),
    'speed_difference': (0.5, 3.0),
    'heading_difference': (5.0, 30.0),
    'gnss_uere_m': 2.0,
    'variance_floor_m2': 0.25,
    'projection_slack_m': 100.0,
    'projection_speed_factor': 2.0,
    'max_speed_ms': 160 / 3.6,
    'iforest_trees': 100,
    'iforest_samples': 256,
    'iforest_seed': 42,
    'iforest_quantiles': (0.95, 0.995),
    'motion_ml_weight': 0.5,
}
REQUIRED = ['timestamp', 'latitude', 'longitude', 'speed', 'heading', 'satellites_used', 'hdop', 'fix_status', 'crowd_latitude', 'crowd_longitude', 'crowd_num_phones', 'estimated_latitude', 'estimated_longitude', 'estimated_speed', 'estimated_heading', 'estimated_pos_var_m2']


class RiskProcessor:
    """Trasformazioni causali e iForest nominale congelata dopo il training."""

    def __init__(self, cfg=None, speed_to_ms=1.0):
        self.cfg = dict(DEFAULT_CFG, **(cfg or {}))
        self.speed_to_ms = speed_to_ms
        self.lat0 = self.lon0 = 0.0
        if not 0 <= self.cfg['motion_ml_weight'] <= 1:
            raise ValueError('motion_ml_weight deve essere in [0,1]')
        q = self.cfg['iforest_quantiles']
        if not 0 <= q[0] < q[1] <= 1:
            raise ValueError('Quantili iForest non validi')
        if not np.isfinite(speed_to_ms) or speed_to_ms <= 0:
            raise ValueError('Fattore velocità non valido')

    # ========================================================================
    # LETTURA E CONTROLLO CSV
    # ========================================================================
    def read_track(self, path):
        return self.prepare_frame(pd.read_csv(path))

    def prepare_frame(self, frame):
        f = frame.copy().reset_index(drop=True)
        missing = set(REQUIRED) - set(f.columns)
        if missing:
            raise ValueError(f'Colonne mancanti {sorted(missing)}')
        for column in REQUIRED:
            if column not in ('timestamp', 'fix_status'):
                f[column] = pd.to_numeric(f[column], errors='raise').astype(float)
        f['timestamp'] = pd.to_datetime(f.timestamp, utc=True)
        if f.timestamp.isna().any() or len(f) < 3:
            raise ValueError('Timestamp non validi o pochi campioni')
        steps = f.timestamp.diff().dt.total_seconds().dropna()
        if (steps <= 0).any() or not np.allclose(steps, steps.median(), rtol=0.01):
            raise ValueError('Servono timestamp crescenti e campionamento regolare; sincronizzare prima le fonti')
        f['speed'] = f.speed * self.speed_to_ms
        f['estimated_speed'] = f.estimated_speed * self.speed_to_ms
        return f

    # ========================================================================
    # CONVERSIONE ENU
    # ========================================================================
    def enu(self, lat, lon, lat0, lon0):
        return np.column_stack((np.radians(np.asarray(lon) - lon0) * EARTH_R * np.cos(np.radians(lat0)), np.radians(np.asarray(lat) - lat0) * EARTH_R))

    # ========================================================================
    # SCALATURA IN [0,1]
    # ========================================================================
    def scale(self, x, limits):
        a, b = limits
        if b <= a:
            raise ValueError('Soglie non ordinate')
        return np.clip((np.asarray(x) - a) / (b - a), 0.0, 1.0)

    # ========================================================================
    # SMOOTHING CAUSALE
    # ========================================================================
    def smooth(self, x, n):
        return pd.DataFrame(x).rolling(n, min_periods=n).mean().to_numpy()

    # ========================================================================
    # DERIVATA CAUSALE
    # ========================================================================
    def past_slope(self, x, n, dt):
        t = np.arange(n) * dt
        t -= t.mean()

        def slope(a):
            return np.dot(a, t) / np.dot(t, t)
        return pd.DataFrame(x).rolling(n, min_periods=n).apply(slope, raw=True).to_numpy()

    # ========================================================================
    # PROIEZIONE SULLA MAPPA
    # ========================================================================
    def project(self, points, geometry, lat0, lon0, dt, max_speed):
        line = self.enu(geometry.latitude, geometry.longitude, lat0, lon0)
        a = line[:-1]
        delta = np.diff(line, axis=0)
        length = np.linalg.norm(delta, axis=1)
        valid = length > 0
        a, delta, length = (a[valid], delta[valid], length[valid])
        starts = geometry.distance_m.to_numpy()[:-1][valid]
        out = np.full((len(points), 4), np.nan)
        previous = None
        previous_i = None
        for i, p in enumerate(points):
            if not np.isfinite(p).all():
                continue
            if previous is None:
                indices = np.arange(len(a))
            else:
                reach = self.cfg['projection_slack_m'] + self.cfg['projection_speed_factor'] * max_speed * dt * (i - previous_i)
                indices = np.flatnonzero((starts <= previous + reach) & (starts + length >= previous - reach))
            if len(indices) == 0:
                continue
            fraction = np.clip(np.sum((p - a[indices]) * delta[indices], axis=1) / length[indices] ** 2, 0, 1)
            projected = a[indices] + fraction[:, None] * delta[indices]
            distance = np.linalg.norm(p - projected, axis=1)
            j = int(np.argmin(distance))
            k = indices[j]
            along = starts[k] + fraction[j] * length[k]
            tangent = delta[k] / length[k]
            out[i] = [distance[j], along, tangent[0], tangent[1]]
            previous, previous_i = (along, i)
        return out

    # ========================================================================
    # 1. POCHI SATELLITI
    # ========================================================================
    def satellite_risk(self, f):
        count = pd.to_numeric(f.satellites_used, errors='coerce')
        valid = np.isfinite(count) & (count >= 0)
        score = 1 - self.scale(count, (self.cfg['sat_critical'], self.cfg['sat_good']))
        return (pd.Series(score, index=f.index).where(valid), valid)

    # ========================================================================
    # 2. CARATTERISTICHE DEL MOTO
    # ========================================================================
    def motion_features(self, f):
        dt = float(f.timestamp.diff().dt.total_seconds().median())
        p = self.enu(f.latitude, f.longitude, self.lat0, self.lon0)
        p[f.fix_status.eq('NO_FIX')] = np.nan
        ns = max(1, round(self.cfg['smooth_s'] / dt))
        n = max(3, round(self.cfg['window_s'] / dt))
        sm = self.smooth(p, ns)
        v = np.vstack(([np.nan, np.nan], np.diff(sm, axis=0) / dt))
        speed = np.linalg.norm(v, axis=1)
        acceleration = np.r_[np.nan, np.diff(speed) / dt]
        jerk = np.r_[np.nan, np.diff(acceleration) / dt]
        heading = np.arctan2(v[:, 0], v[:, 1])
        turn = np.r_[np.nan, np.angle(np.exp(1j * np.diff(heading))) / dt]
        lateral = np.abs(speed * turn)
        lateral[speed < self.cfg['min_motion_ms']] = 0.0
        pred = np.full_like(p, np.nan)
        pred[2:] = 2 * p[1:-1] - p[:-2]
        raw = pd.DataFrame(dict(acceleration=np.abs(acceleration), jerk=np.abs(jerk), lateral=lateral, prediction=np.linalg.norm(p - pred, axis=1)), index=f.index)
        windows = pd.concat([raw.rolling(n, min_periods=n).median().add_suffix('_median'), raw.rolling(n, min_periods=n).max().add_suffix('_max')], axis=1)
        return (raw, windows)

    # ========================================================================
    # 2. RISCHIO FISICO E ISOLATION FOREST
    # ========================================================================
    def motion_risk(self, f):
        raw, windows = self.motion_features(f)
        physical = pd.DataFrame({c: self.scale(raw[c], self.cfg[c]) for c in raw}, index=f.index).max(axis=1)
        valid = windows.notna().all(axis=1)
        ml = pd.Series(np.nan, index=f.index)
        if valid.any():
            ml.loc[valid] = self.scale(-self.iforest.score_samples((windows.loc[valid] - self.center) / self.spread), (self.q_low, self.q_high))
        combined = physical.copy()
        combined.loc[valid] = (1 - self.cfg['motion_ml_weight']) * physical.loc[valid] + self.cfg['motion_ml_weight'] * ml.loc[valid]
        return (combined, physical, ml, raw)

    # ========================================================================
    # 3. DIVERGENZA CROWD
    # ========================================================================
    def crowd_risk(self, f, gps, crowd, state, tangent, dt):
        residual = gps - crowd
        body = np.column_stack((np.sum(residual * tangent, axis=1), residual[:, 0] * tangent[:, 1] - residual[:, 1] * tangent[:, 0]))
        valid = (f.crowd_num_phones.to_numpy() >= self.cfg['crowd_min_phones']) & np.isfinite(body).all(axis=1)
        body[~valid] = np.nan
        n = max(3, round(self.cfg['window_s'] / dt))
        dynamic = self.scale(np.linalg.norm(self.past_slope(body, n, dt), axis=1), self.cfg['crowd_rate'])
        variance = np.maximum(self.cfg['variance_floor_m2'], (f.hdop.to_numpy() * self.cfg['gnss_uere_m']) ** 2 + f.estimated_pos_var_m2.to_numpy())
        d2 = np.sum((gps - state) ** 2, axis=1) / variance
        offset = np.full_like(body, np.nan)
        initial = []
        baseline = None
        required = max(3, round(self.cfg['baseline_s'] / dt))
        for i, row in enumerate(body):
            if baseline is None:
                if valid[i] and np.isfinite(d2[i]) and (d2[i] < self.cfg['state_d2'][0]):
                    initial.append(row)
                    if len(initial) >= required:
                        baseline = np.median(initial, axis=0)
                else:
                    initial = []
            if baseline is not None and valid[i]:
                offset[i] = row - baseline
        persistent = self.scale(np.linalg.norm(offset, axis=1), self.cfg['crowd_offset'])
        parts = pd.DataFrame(dict(dynamic=dynamic, persistent=persistent), index=f.index)
        return (parts.max(axis=1), parts)

    # ========================================================================
    # 4. DIVERGENZA DALLA MAPPA
    # ========================================================================
    def map_risk(self, gps, state_map, geometry, dt):
        projected = self.project(gps, geometry, self.lat0, self.lon0, dt, self.cfg['max_speed_ms'])
        cross = self.scale(projected[:, 0], self.cfg['map_distance'])
        progress = self.scale(np.abs(projected[:, 1] - state_map[:, 1]), self.cfg['map_progress'])
        parts = pd.DataFrame(dict(cross_track=cross, progress=progress))
        return (parts.max(axis=1), parts)

    # ========================================================================
    # 5. DISACCORDO SENSORI
    # ========================================================================
    def sensor_risk(self, f, gps, state):
        var = f.estimated_pos_var_m2.to_numpy()
        if np.any(np.isfinite(var) & (var < 0)):
            raise ValueError('Varianza dello stato negativa')
        variance = np.maximum(self.cfg['variance_floor_m2'], (f.hdop.to_numpy() * self.cfg['gnss_uere_m']) ** 2 + var)
        d2 = np.sum((gps - state) ** 2, axis=1) / variance
        position = self.scale(d2, self.cfg['state_d2'])
        speed = self.scale(np.abs(f.speed - f.estimated_speed), self.cfg['speed_difference'])
        angle = np.abs((f.heading - f.estimated_heading + 180) % 360 - 180)
        heading = self.scale(angle, self.cfg['heading_difference'])
        heading[f.estimated_speed.abs().to_numpy() < self.cfg['min_motion_ms']] = np.nan
        parts = pd.DataFrame(dict(position=position, speed=speed, heading=heading), index=f.index)
        parts.loc[f.fix_status.eq('NO_FIX')] = np.nan
        return (parts.max(axis=1), parts)

    # ========================================================================
    # TRAINING: esclusivamente sequenze interamente nominali del train
    # ========================================================================
    def fit(self, paths):
        windows = []
        nominal_files = 0
        for path in paths:
            f = self.read_track(path)
            if 'anomaly_label' not in f:
                raise ValueError(f'{path}: anomaly_label assente')
            if not f.anomaly_label.eq('normal').all():
                continue
            self.lat0, self.lon0 = float(f.latitude.iloc[0]), float(f.longitude.iloc[0])
            windows.append(self.motion_features(f)[1].dropna())
            nominal_files += 1
        if not windows:
            raise ValueError('Nessun CSV interamente nominale nel train')
        train = pd.concat(windows, ignore_index=True)
        if len(train) < 20:
            raise ValueError('Servono almeno 20 finestre nominali complete')
        self.center = train.median()
        self.spread = (train.quantile(.75)-train.quantile(.25)).clip(lower=1e-6)
        self.iforest = IsolationForest(n_estimators=self.cfg['iforest_trees'],
            max_samples=min(self.cfg['iforest_samples'], len(train)),
            random_state=self.cfg['iforest_seed'], n_jobs=1)
        self.iforest.fit((train-self.center)/self.spread)
        reference = -self.iforest.score_samples((train-self.center)/self.spread)
        self.q_low, self.q_high = np.quantile(reference, self.cfg['iforest_quantiles'])
        self.q_high = max(self.q_high, self.q_low+1e-9)
        print(f'iForest: {nominal_files} CSV nominali train, {len(train)} finestre')
        return self

    # ========================================================================
    # ELABORAZIONE DI UNA ROTTA: nessuna scrittura accessoria
    # ========================================================================
    def transform(self, frame, geometry):
        if not hasattr(self, 'iforest'):
            raise ValueError('Eseguire fit prima di transform')
        original = frame.copy().reset_index(drop=True)
        f = self.prepare_frame(original)
        columns = ['latitude','longitude','distance_m']
        if not set(columns) <= set(geometry) or len(geometry) < 2:
            raise ValueError('Geometria non valida')
        if not np.isfinite(geometry[columns].to_numpy()).all() or not geometry.distance_m.is_monotonic_increasing:
            raise ValueError('Geometria non ordinata o non finita')
        self.lat0, self.lon0 = float(geometry.latitude.iloc[0]), float(geometry.longitude.iloc[0])
        dt = float(f.timestamp.diff().dt.total_seconds().median())
        gps = self.enu(f.latitude, f.longitude, self.lat0, self.lon0)
        gps[f.fix_status.eq('NO_FIX')] = np.nan
        state = self.enu(f.estimated_latitude, f.estimated_longitude, self.lat0, self.lon0)
        crowd = self.enu(f.crowd_latitude, f.crowd_longitude, self.lat0, self.lon0)
        state_map = self.project(state, geometry, self.lat0, self.lon0, dt, self.cfg['max_speed_ms'])
        risks = pd.DataFrame(index=f.index)
        risks['satellites'] = self.satellite_risk(f)[0]
        risks['motion'] = self.motion_risk(f)[0]
        risks['crowd'] = self.crowd_risk(f, gps, crowd, state, state_map[:,2:4], dt)[0]
        risks['map'] = self.map_risk(gps, state_map, geometry, dt)[0]
        risks['sensors'] = self.sensor_risk(f, gps, state)[0]
        result = pd.concat([original[['timestamp']], risks.add_prefix('risk_')], axis=1)
        if 'anomaly_label' in original:
            result['anomaly_label'] = original.anomaly_label
        numeric = result.filter(regex='^risk_')
        if np.isinf(numeric.to_numpy()).any() or not numeric.stack().dropna().between(0,1).all():
            raise ValueError('Rischi fuori intervallo')
        if len(result) != len(original):
            raise ValueError('Numero di campioni alterato')
        return result

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path):
        processor = joblib.load(path)
        if not isinstance(processor, cls) or not hasattr(processor, 'iforest'):
            raise ValueError('Artefatto preprocessing non valido')
        return processor
