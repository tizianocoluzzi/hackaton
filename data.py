#!/usr/bin/env python3
"""
train_gnss_generator.py  (v2: + IMU and crowdsourced phone GNSS)
================================================================
Synthetic train GNSS track generator with tunable receiver errors and
injectable anomalies (spoofing, jamming, low satellite count, multipath, tunnel),
plus two *independent* sensor sources generated from the TRUE (non-altered)
train motion:

  * an on-board IMU (accelerometer + yaw-rate gyro), never touched by GNSS anomalies
  * crowdsourced GNSS from passengers' phones (aggregated into one "average" position)

Output CSV columns (this order)
-------------------------------
Original receiver block (always present):
    timestamp, latitude, longitude, speed, heading, satellites_used, hdop,
    fix_status, signal_quality, reference_position_available, anomaly_label

IMU block (disable with --no-imu):
    accel_x    longitudinal specific force [m/s^2], +forward (acceleration > 0, braking < 0)
    accel_y    lateral specific force [m/s^2], +right. Includes track cant (banking),
               so in a curve it is  v^2*k*cos(phi) - g*sin(phi)
    accel_z    vertical specific force [m/s^2], +up (~ +9.81 at rest; includes vibration)
    gyro_z     yaw rate [deg/s], +clockwise seen from above (right turn > 0), = v * curvature
    vib_rms    RMS of broadband vibration over the epoch [m/s^2] (grows with speed)

Crowdsourced block (disable with --no-crowd):
    crowd_latitude, crowd_longitude   aggregate (mean | median | trimmed) of the phone fixes
    crowd_speed                       mean phone speed (same unit as `speed`)
    crowd_heading                     circular mean of phone headings [deg]
    crowd_num_phones                  phones that delivered a valid fix at that epoch
    crowd_pos_std_m                   dispersion of phone positions around the aggregate [m]
    crowd_accuracy_m                  mean horizontal accuracy reported by the phones [m]
    (empty fields when no phone has a fix, e.g. in a tunnel)

With --with-truth: truth_latitude, truth_longitude, truth_speed are appended.

Crowd model
-----------
Up to --crowd-max-phones phone "slots" ride in a train of --train-length-m. Passenger
occupancy changes at every station stop (some alight, some board). Each phone sits at
a random place along/across the train (so its true position is offset from the
antenna reference, which is at the train centre unless --antenna-offset-m is set),
has its own correlated error (lognormal spread around --phone-sigma-m), random
dropouts, and occasional outliers (stale / network fixes). Phones are independent
receivers: by default only `tunnel` affects them (--crowd-affected-by selects which
anomalies also hit them: tunnel, jamming, multipath, spoofing).

Quick examples
--------------
  # 5 mixed tracks, 3 random anomalies each (IMU + crowd included)
  python train_gnss_generator.py --num-tracks 5 --random-anomalies 3 --seed 42

  # spoofing that does NOT affect phones, robust median aggregation
  python train_gnss_generator.py --track-type s_curve --length-km 30 \
      --anomaly "spoofing:300:120:mode=drift,magnitude=400,ramp=90" \
      --anomaly "jamming:700:90:peak=1.0,cn0_drop=28" \
      --crowd-aggregate median

  # spoofer also hits the phones, busier train, noisier phones
  python train_gnss_generator.py --crowd-affected-by tunnel spoofing \
      --crowd-mean-phones 30 --phone-sigma-m 9

  # original 11-column format only
  python train_gnss_generator.py --no-imu --no-crowd

Anomaly spec syntax:  kind:start_s:duration_s[:key=value,key=value,...]

Anomaly kinds and their parameters
----------------------------------
spoofing        mode=drift|jump (drift)   magnitude=<m> (300)   ramp=<s> (0.7*dur)
                direction=<deg> (random)  consistent_velocity=0|1 (0)
                stealth=0..1 (0)          cn0_boost=<dB> (4)    sats=<n> (nominal)
                -> position offset; with stealth<0.5 also an unnaturally strong,
                   flat signal (high C/N0, constant sat count, low HDOP).
jamming         peak=0..1 (1)  ramp=<s> (0.3*dur)  cn0_drop=<dB> (25)
                no_fix_threshold=0..1 (0.7)  pos_sigma=<m> (20)
                -> C/N0 collapse, satellites lost, HDOP up, fix lost at high intensity.
low_satellites  min_sats=<n> (3)  max_sats=<n> (5)  hdop_mult=<x> (1.5)
                cn0_delta=<dB> (-2)
                -> few satellites, poor geometry, signal level only mildly affected.
multipath       sigma=<m> (8)  sat_loss=<n> (3)  hdop_mult=<x> (1.8)  cn0_delta=<dB> (-5)
                -> short-correlated position errors, degraded HDOP, jittery C/N0.
tunnel          cn0=<dB> (5)
                -> total signal loss (0 satellites, NO_FIX).
"""

from __future__ import annotations

import argparse
import copy
import csv
import dataclasses
import json
import math
import os
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

EARTH_R = 6378137.0  # WGS84 equatorial radius [m]
G = 9.80665          # standard gravity [m/s^2]

COLUMNS = [
    "timestamp", "latitude", "longitude", "speed", "heading",
    "satellites_used", "hdop", "fix_status", "signal_quality",
    "reference_position_available", "anomaly_label",
]
IMU_COLUMNS = ["accel_x", "accel_y", "accel_z", "gyro_z", "vib_rms"]
CROWD_COLUMNS = ["crowd_latitude", "crowd_longitude", "crowd_speed", "crowd_heading",
                 "crowd_num_phones", "crowd_pos_std_m", "crowd_accuracy_m"]
TRUTH_COLUMNS = ["truth_latitude", "truth_longitude", "truth_speed"]

TRACK_TYPES = ["straight", "curve", "s_curve", "mixed", "loop"]
ANOMALY_TYPES = ["spoofing", "jamming", "low_satellites", "multipath", "tunnel"]
ANOMALY_ALIASES = {"spoof": "spoofing", "jam": "jamming", "low_sats": "low_satellites",
                   "lowsats": "low_satellites", "low_satelites": "low_satellites"}
SPEED_UNITS = {"ms": 1.0, "kmh": 3.6, "knots": 1.943844}
CROWD_AGGREGATES = ["mean", "median", "trimmed"]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class TrackConfig:
    # --- track / train dynamics ---
    track_type: str = "mixed"
    length_km: float = 20.0
    duration_s: Optional[float] = None      # truncate the run after this many seconds
    rate_hz: float = 1.0
    start_lat: float = 45.0
    start_lon: float = 9.0
    start_heading_deg: Optional[float] = None
    start_time: datetime = field(default_factory=lambda: datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc))
    max_speed_kmh: float = 160.0
    accel: float = 0.7                      # m/s^2
    decel: float = 0.8                      # m/s^2
    lat_accel: float = 0.9                  # max lateral acceleration in curves, m/s^2
    n_stations: Optional[int] = None        # intermediate stops (default: ~1 per 8 km)
    dwell_s: Tuple[float, float] = (30.0, 60.0)
    initial_dwell_s: float = 15.0

    # --- reference position availability (balises / stations) ---
    balise_spacing_m: float = 1500.0
    ref_radius_m: float = 60.0

    # --- receiver error model ("tunable error") ---
    error_scale: float = 1.0                # global multiplier on pos/speed/heading noise
    pos_noise_m: float = 1.5                # 1-sigma horizontal error at nominal HDOP
    noise_corr_s: float = 30.0              # correlation time of the position error
    speed_noise_ms: float = 0.05
    heading_noise_deg: float = 0.5
    sats_nominal: float = 14.0
    hdop_nominal: float = 0.9
    cn0_nominal: float = 42.0               # dB-Hz
    cn0_noise: float = 1.0
    base_fix: str = "3D"                    # fix_status label used when healthy

    # --- IMU (accelerometer + gyro), generated from the true motion ---
    enable_imu: bool = True
    imu_error_scale: float = 1.0            # global multiplier on IMU noise / bias
    imu_noise_ms2: float = 0.02             # white accel noise per epoch (1-sigma)
    imu_bias_ms2: float = 0.02              # constant per-track accel bias (1-sigma)
    gyro_noise_dps: float = 0.05            # white gyro noise [deg/s]
    gyro_bias_dps: float = 0.02             # constant gyro bias (1-sigma) [deg/s]
    vib_base_ms2: float = 0.02              # vibration RMS floor while moving
    vib_per_ms: float = 0.0035              # additional vibration RMS per m/s of speed
    cant_max_deg: float = 6.0               # max track cant (banking) in curves

    # --- crowdsourced phones ---
    enable_crowd: bool = True
    crowd_max_phones: int = 40              # simulated phone slots
    crowd_mean_phones: float = 15.0         # typical number of phones on board
    crowd_boarding_frac: float = 0.3        # fraction of passengers changing at each stop
    train_length_m: float = 200.0
    antenna_offset_m: float = 0.0           # antenna position from train centre (+ = front)
    phone_sigma_m: float = 6.0              # median 1-sigma phone error per axis
    phone_corr_s: float = 20.0              # phone error correlation time
    phone_dropout: float = 0.10             # per-epoch probability a phone gives no fix
    phone_outlier_prob: float = 0.02        # per-epoch probability of a gross outlier
    phone_outlier_m: Tuple[float, float] = (30.0, 200.0)
    phone_speed_noise: float = 0.3          # m/s
    phone_heading_noise: float = 6.0        # deg
    crowd_aggregate: str = "mean"           # mean | median | trimmed
    crowd_affected_by: List[str] = field(default_factory=lambda: ["tunnel"])

    # --- anomalies ---
    n_random_anomalies: int = 0
    anomaly_kinds: List[str] = field(default_factory=lambda: list(ANOMALY_TYPES))
    anomaly_duration: Tuple[float, float] = (30.0, 120.0)
    no_fix_output: str = "hold"             # hold | nan | noisy


@dataclass
class AnomalySpec:
    kind: str
    start: float
    duration: float
    params: Dict[str, object] = field(default_factory=dict)

    @property
    def end(self) -> float:
        return self.start + self.duration

    def p(self, key: str, default):
        return self.params.get(key, default)


@dataclass
class Effects:
    """Per-epoch modifications requested by the active anomalies."""
    sats_set: Optional[int] = None
    sats_mult: float = 1.0
    sats_delta: float = 0.0
    hdop_mult: float = 1.0
    hdop_set: Optional[float] = None
    cn0_delta: float = 0.0
    cn0_set: Optional[float] = None
    cn0_noise_mult: float = 1.0
    extra_sigma_m: float = 0.0
    offset_e: float = 0.0
    offset_n: float = 0.0
    force_fix: Optional[str] = None
    velocity_from_pos: bool = False


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def enu_to_latlon(e, n, lat0, lon0):
    lat = lat0 + np.degrees(np.asarray(n) / EARTH_R)
    lon = lon0 + np.degrees(np.asarray(e) / (EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def ar1(n: int, tau_s: float, dt: float, rng: np.random.Generator) -> np.ndarray:
    """Unit-variance AR(1) (Gauss-Markov) noise with correlation time tau_s."""
    phi = math.exp(-dt / max(tau_s, 1e-6))
    sd = math.sqrt(1.0 - phi * phi)
    w = rng.standard_normal(n)
    out = np.empty(n)
    out[0] = w[0]
    for i in range(1, n):
        out[i] = phi * out[i - 1] + sd * w[i]
    return out


def ar1_multi(m: int, n: int, tau_s: float, dt: float, rng: np.random.Generator) -> np.ndarray:
    """m independent unit-variance AR(1) series of length n, shape (m, n)."""
    phi = math.exp(-dt / max(tau_s, 1e-6))
    sd = math.sqrt(1.0 - phi * phi)
    w = rng.standard_normal((m, n))
    out = np.empty((m, n))
    out[:, 0] = w[:, 0]
    for i in range(1, n):
        out[:, i] = phi * out[:, i - 1] + sd * w[:, i]
    return out


# --------------------------------------------------------------------------- #
# Track geometry
# --------------------------------------------------------------------------- #
def build_path(track_type: str, length_m: float, rng: np.random.Generator,
               ds: float, heading0: float):
    """Return arc-length s, ENU x (east), y (north), heading h (rad, cw from N), curvature k."""
    n = int(round(length_m / ds)) + 1
    sgn = lambda: float(rng.choice([-1.0, 1.0]))
    segs: List[Tuple[float, float]] = []

    if track_type == "straight":
        segs = [(length_m, 0.0)]
    elif track_type == "curve":
        R = rng.uniform(800, 3000)
        arc = min(R * rng.uniform(math.radians(40), math.radians(110)), 0.6 * length_m)
        pre = (length_m - arc) * rng.uniform(0.3, 0.7)
        segs = [(pre, 0.0), (arc, sgn() / R), (length_m - pre - arc, 0.0)]
    elif track_type == "s_curve":
        s = sgn()
        R1, R2 = rng.uniform(800, 3000, 2)
        a1 = min(R1 * rng.uniform(math.radians(30), math.radians(80)), 0.25 * length_m)
        a2 = min(R2 * rng.uniform(math.radians(30), math.radians(80)), 0.25 * length_m)
        rest = length_m - a1 - a2
        segs = [(0.3 * rest, 0.0), (a1, s / R1), (0.2 * rest, 0.0), (a2, -s / R2), (0.5 * rest, 0.0)]
    elif track_type == "mixed":
        total = 0.0
        while total < length_m:
            st = rng.uniform(500, 3000)
            R = rng.uniform(500, 4000)
            arc = R * rng.uniform(math.radians(10), math.radians(80))
            segs += [(st, 0.0), (arc, sgn() / R)]
            total += st + arc
    elif track_type == "loop":
        segs = [(length_m, 2.0 * math.pi / length_m)]
    else:
        raise ValueError(f"unknown track type: {track_type}")

    k = np.zeros(n)
    pos = 0
    for L, c in segs:
        m = int(round(L / ds))
        k[pos:pos + m] = c
        pos += m
        if pos >= n:
            break

    # smooth curvature -> clothoid-like transitions (no instantaneous heading-rate steps)
    w = max(3, int(200.0 / ds) | 1)
    k = np.convolve(np.pad(k, (w // 2, w // 2), mode="edge"), np.ones(w) / w, mode="valid")[:n]

    h = heading0 + np.concatenate([[0.0], np.cumsum(k[:-1]) * ds])
    x = np.concatenate([[0.0], np.cumsum(np.sin(h[:-1]) * ds)])
    y = np.concatenate([[0.0], np.cumsum(np.cos(h[:-1]) * ds)])
    s = np.arange(n) * ds
    return s, x, y, h, k


def plan_stops(cfg: TrackConfig, n: int, ds: float, rng: np.random.Generator) -> List[int]:
    L_km = cfg.length_km
    m = cfg.n_stations if cfg.n_stations is not None else max(0, int(L_km / 8.0) - 1)
    idx = []
    for j in range(m):
        base = (j + 1) / (m + 1)
        jitter = rng.uniform(-0.3, 0.3) / (m + 1)
        idx.append(int(round((base + jitter) * (n - 1))))
    idx = sorted({i for i in idx if 0 < i < n - 1})
    return idx + [n - 1]


def speed_limit_profile(k, ds, vmax, lat_acc, decel, stop_idx):
    vlim = np.minimum(vmax, np.sqrt(lat_acc / np.maximum(np.abs(k), 1e-9)))
    for i in stop_idx:
        vlim[i] = 0.0
    for i in range(len(vlim) - 2, -1, -1):  # backward pass: braking envelope
        vlim[i] = min(vlim[i], math.sqrt(vlim[i + 1] ** 2 + 2.0 * decel * ds))
    return vlim


def simulate_truth(cfg: TrackConfig, s, vlim, stop_idx, rng):
    """Time-step the train along the path. Returns arrays t, s, v."""
    dt = 1.0 / cfg.rate_hz
    ds = s[1] - s[0]
    stop_s = [s[i] for i in stop_idx]
    ptr, pos, v, t = 0, 0.0, 0.0, 0.0
    dwell = cfg.initial_dwell_s
    finished = False
    ts, ss, vs = [], [], []
    while True:
        ts.append(t); ss.append(pos); vs.append(v)
        if finished:
            break
        if cfg.duration_s is not None and t + dt > cfg.duration_s:
            break
        if dwell > 0:
            dwell -= dt
            v = 0.0
            t += dt
            continue
        # floor at 1 m/s: the limit is 0 exactly on a stop cell, which would block departure
        vl = max(vlim[min(int(pos / ds), len(vlim) - 1)], 1.0)
        v_old = v
        v = min(vl, v + cfg.accel * dt) if v < vl else max(vl, v - cfg.decel * dt)
        # never arrive at a stop faster than the service brake can handle (keeps accelerations physical)
        remaining = stop_s[ptr] - (pos + 0.5 * (v_old + v) * dt)   # look one step ahead
        v = min(v, max(math.sqrt(2.0 * cfg.decel * max(remaining, 0.0)), 0.5))
        pos += 0.5 * (v_old + v) * dt
        t += dt
        if pos >= stop_s[ptr] - 0.5:
            pos, v = stop_s[ptr], 0.0
            if ptr == len(stop_s) - 1:
                finished = True
            else:
                dwell = rng.uniform(*cfg.dwell_s)
            ptr += 1
    return np.array(ts), np.array(ss), np.array(vs), stop_s


# --------------------------------------------------------------------------- #
# IMU (accelerometer + gyro) from true kinematics
# --------------------------------------------------------------------------- #
def simulate_imu(cfg: TrackConfig, s, k, ss, vt, rng: np.random.Generator) -> Dict[str, np.ndarray]:
    """Specific-force / yaw-rate measurements consistent with the TRUE motion.

    Body axes: x forward, y right, z up.  Track cant phi (banking toward the inside of
    the curve) rotates gravity into the lateral axis:
        f_x = dv/dt
        f_y = v^2 k cos(phi) - g sin(phi)
        f_z = g cos(phi) + v^2 k sin(phi)  (+ vibration)
    """
    N = len(ss)
    dt = 1.0 / cfg.rate_hz
    sc = cfg.imu_error_scale

    kt = np.interp(ss, s, k)
    a_long = np.gradient(vt, dt) if N > 1 else np.zeros(N)
    yaw = vt * kt                                   # rad/s, + = right turn
    a_c = vt ** 2 * kt                              # centripetal accel, +right

    v_ref = 0.8 * cfg.max_speed_kmh / 3.6           # cant designed for ~80 % of max speed
    cant = np.clip(0.6 * np.arctan(kt * v_ref ** 2 / G),
                   -math.radians(cfg.cant_max_deg), math.radians(cfg.cant_max_deg))

    fx = a_long
    fy = a_c * np.cos(cant) - G * np.sin(cant)
    fz = G * np.cos(cant) + a_c * np.sin(cant)

    # sensor errors: constant bias per axis (+ slow wander) and white noise
    bias = rng.normal(0.0, cfg.imu_bias_ms2 * sc, 3)
    wander = [0.3 * cfg.imu_bias_ms2 * sc * ar1(N, 300.0, dt, rng) for _ in range(3)]
    vib_rms = np.where(vt > 0.5, cfg.vib_base_ms2 + cfg.vib_per_ms * vt, 0.3 * cfg.vib_base_ms2)
    vib_rms = vib_rms * (1.0 + 0.1 * ar1(N, 20.0, dt, rng)) * sc if sc > 0 else vib_rms * 0.0
    white = cfg.imu_noise_ms2 * sc

    ax = fx + bias[0] + wander[0] + 0.5 * vib_rms * rng.standard_normal(N) + white * rng.standard_normal(N)
    ay = fy + bias[1] + wander[1] + 1.0 * vib_rms * rng.standard_normal(N) + white * rng.standard_normal(N)
    az = fz + bias[2] + wander[2] + 1.5 * vib_rms * rng.standard_normal(N) + white * rng.standard_normal(N)

    gbias = rng.normal(0.0, cfg.gyro_bias_dps * sc)
    gz = np.degrees(yaw) + gbias + cfg.gyro_noise_dps * sc * rng.standard_normal(N) \
        + 0.2 * np.degrees(vib_rms) * rng.standard_normal(N)

    return {"accel_x": ax, "accel_y": ay, "accel_z": az, "gyro_z": gz, "vib_rms": vib_rms}


# --------------------------------------------------------------------------- #
# Crowdsourced phones
# --------------------------------------------------------------------------- #
def simulate_crowd(cfg: TrackConfig, s, x, y, h, ss, vt, th, ctl, rng: np.random.Generator):
    """Aggregate GNSS of the passengers' phones, built from the TRUE train motion.

    `ctl` holds per-epoch arrays describing how the on-board anomalies leak into the phones:
    valid_p (probability a phone has a fix), sig_mult (error inflation), off_e / off_n (spoofing offset).
    """
    M = max(1, cfg.crowd_max_phones)
    N = len(ss)
    dt = 1.0 / cfg.rate_hz
    L = cfg.train_length_m

    # ---- occupancy: constant while moving, changes during each station stop ----
    still = vt < 0.05
    starts = [0] + [i for i in range(1, N) if still[i] and not still[i - 1]]
    state = np.zeros(M, dtype=bool)
    along = np.zeros(M)       # position along the train relative to the antenna (+ = front)
    lat_off = np.zeros(M)     # lateral position in the carriage [m]
    sig = np.zeros(M)         # per-phone 1-sigma error

    def board(idx):
        along[idx] = rng.uniform(-L / 2, L / 2, len(idx)) - cfg.antenna_offset_m
        lat_off[idx] = rng.uniform(-1.4, 1.4, len(idx))
        sig[idx] = cfg.phone_sigma_m * rng.lognormal(0.0, 0.4, len(idx))

    n0 = int(np.clip(rng.poisson(cfg.crowd_mean_phones), 1, M))
    first = rng.choice(M, n0, replace=False)
    state[first] = True
    board(first)

    mask = np.zeros((M, N), dtype=bool)
    along_a = np.zeros((M, N))
    lat_a = np.zeros((M, N))
    sig_a = np.zeros((M, N))
    bounds = starts + [N]
    for j, b in enumerate(starts):
        if j > 0:
            act = np.flatnonzero(state)
            n_al = int(round(len(act) * rng.uniform(0.1, 0.5)))
            if n_al:
                state[rng.choice(act, n_al, replace=False)] = False
            free = np.flatnonzero(~state)
            n_bd = min(int(rng.poisson(max(0.0, cfg.crowd_mean_phones * cfg.crowd_boarding_frac))), len(free))
            if n_bd:
                new = rng.choice(free, n_bd, replace=False)
                state[new] = True
                board(new)
            if not state.any():
                new = rng.choice(M, 1)
                state[new] = True
                board(new)
        e = bounds[j + 1]
        mask[:, b:e] = state[:, None]
        along_a[:, b:e] = along[:, None]
        lat_a[:, b:e] = lat_off[:, None]
        sig_a[:, b:e] = sig[:, None]

    # ---- true phone positions (offset along the track and across the carriage) ----
    sph = np.clip(ss[None, :] + along_a, 0.0, s[-1])
    xe = np.interp(sph.ravel(), s, x).reshape(M, N)
    yn = np.interp(sph.ravel(), s, y).reshape(M, N)
    hp = np.interp(sph.ravel(), s, h).reshape(M, N)
    pe = xe + lat_a * np.cos(hp)
    pn = yn - lat_a * np.sin(hp)

    # ---- phone errors ----
    sm = ctl["sig_mult"][None, :]
    pe = pe + sig_a * sm * ar1_multi(M, N, cfg.phone_corr_s, dt, rng) + ctl["off_e"][None, :]
    pn = pn + sig_a * sm * ar1_multi(M, N, cfg.phone_corr_s, dt, rng) + ctl["off_n"][None, :]
    out = rng.random((M, N)) < cfg.phone_outlier_prob
    mag = rng.uniform(cfg.phone_outlier_m[0], cfg.phone_outlier_m[1], (M, N))
    ang = rng.uniform(0.0, 2 * math.pi, (M, N))
    pe = pe + out * mag * np.sin(ang)
    pn = pn + out * mag * np.cos(ang)

    valid = mask & (rng.random((M, N)) >= cfg.phone_dropout) \
        & (rng.random((M, N)) < ctl["valid_p"][None, :])
    cnt = valid.sum(axis=0)

    E = np.where(valid, pe, np.nan)
    Nn = np.where(valid, pn, np.nan)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        if cfg.crowd_aggregate == "median":
            Em, Nm = np.nanmedian(E, axis=0), np.nanmedian(Nn, axis=0)
        elif cfg.crowd_aggregate == "trimmed":
            me, mn_ = np.nanmedian(E, axis=0), np.nanmedian(Nn, axis=0)
            dist = np.hypot(E - me, Nn - mn_)
            thr = np.nanquantile(dist, 0.8, axis=0)
            keep = dist <= thr[None, :]
            Em, Nm = np.nanmean(np.where(keep, E, np.nan), axis=0), np.nanmean(np.where(keep, Nn, np.nan), axis=0)
        else:
            Em, Nm = np.nanmean(E, axis=0), np.nanmean(Nn, axis=0)
        spread = np.sqrt(np.nanvar(E, axis=0) + np.nanvar(Nn, axis=0))
        acc = np.nanmean(np.where(valid, 1.2 * sig_a * sm, np.nan), axis=0)

        # speed
        ps = np.maximum(0.0, vt[None, :] + cfg.phone_speed_noise * rng.standard_normal((M, N)))
        ps = np.where(vt[None, :] < 0.1, 0.0, ps)
        spd = np.nanmean(np.where(valid, ps, np.nan), axis=0)

        # heading: circular mean over phones
        hph = hp + np.radians(cfg.phone_heading_noise) * rng.standard_normal((M, N))
        sx = np.where(valid, np.sin(hph), 0.0).sum(axis=0)
        cx = np.where(valid, np.cos(hph), 0.0).sum(axis=0)
        head = np.degrees(np.arctan2(sx, cx)) % 360.0

    # hold heading when stopped / no data (course is undefined at standstill)
    last = float(th[0])
    for i in range(N):
        if cnt[i] == 0 or vt[i] < 0.5:
            head[i] = last
        else:
            last = head[i]

    spread = np.where(cnt >= 2, spread, np.where(cnt == 1, 0.0, np.nan))
    lat, lon = enu_to_latlon(Em, Nm, cfg.start_lat, cfg.start_lon)
    return {"crowd_latitude": lat, "crowd_longitude": lon, "crowd_speed": spd,
            "crowd_heading": head, "crowd_num_phones": cnt,
            "crowd_pos_std_m": spread, "crowd_accuracy_m": acc}


# --------------------------------------------------------------------------- #
# Anomalies
# --------------------------------------------------------------------------- #
def prepare_anomaly(a: AnomalySpec, rng: np.random.Generator):
    if a.kind == "spoofing" and "direction" not in a.params:
        a.params["direction"] = float(rng.uniform(0, 360))


def _spoofing(a, tr, eff, rng, cfg):
    mag = float(a.p("magnitude", 300.0))
    mode = str(a.p("mode", "drift"))
    ramp = float(a.p("ramp", 0.7 * a.duration))
    if mode == "jump":
        f = 1.0
    elif mode == "drift":
        f = min(1.0, tr / max(ramp, 1e-6))
    else:
        raise ValueError("spoofing mode must be 'drift' or 'jump'")
    ang = math.radians(float(a.p("direction", 0.0)))
    eff.offset_e += mag * f * math.sin(ang)
    eff.offset_n += mag * f * math.cos(ang)
    stealth = min(1.0, max(0.0, float(a.p("stealth", 0.0))))
    if stealth < 0.5:  # tell-tale signature: too clean, too strong, too constant
        eff.sats_set = int(a.p("sats", round(cfg.sats_nominal)))
        eff.hdop_set = cfg.hdop_nominal * 0.85
    eff.cn0_delta += float(a.p("cn0_boost", 4.0)) * (1.0 - stealth)
    eff.cn0_noise_mult *= 1.0 - 0.7 * (1.0 - stealth)
    eff.velocity_from_pos = bool(a.p("consistent_velocity", 0))


def _jamming(a, tr, eff, rng, cfg):
    peak = float(a.p("peak", 1.0))
    ramp = min(float(a.p("ramp", 0.3 * a.duration)), a.duration / 2.0)
    ramp = max(ramp, 1e-6)
    if tr < ramp:
        x = tr / ramp
    elif tr > a.duration - ramp:
        x = (a.duration - tr) / ramp
    else:
        x = 1.0
    I = peak * math.sin(0.5 * math.pi * min(1.0, max(0.0, x))) ** 2
    eff.cn0_delta -= float(a.p("cn0_drop", 25.0)) * I
    eff.cn0_noise_mult *= 1.0 + 2.0 * I
    eff.sats_mult *= 1.0 - 0.95 * I
    eff.hdop_mult *= 1.0 + 5.0 * I
    eff.extra_sigma_m += float(a.p("pos_sigma", 20.0)) * I
    if I >= float(a.p("no_fix_threshold", 0.7)):
        eff.force_fix = "NO_FIX"


def _low_satellites(a, tr, eff, rng, cfg):
    lo = int(a.p("min_sats", 3))
    hi = int(a.p("max_sats", 5))
    eff.sats_set = int(rng.integers(lo, hi + 1))
    eff.hdop_mult *= float(a.p("hdop_mult", 1.5))
    eff.cn0_delta += float(a.p("cn0_delta", -2.0))


def _multipath(a, tr, eff, rng, cfg):
    eff.extra_sigma_m += float(a.p("sigma", 8.0))
    eff.sats_delta -= float(a.p("sat_loss", 3.0))
    eff.hdop_mult *= float(a.p("hdop_mult", 1.8))
    eff.cn0_delta += float(a.p("cn0_delta", -5.0))
    eff.cn0_noise_mult *= 3.0


def _tunnel(a, tr, eff, rng, cfg):
    eff.sats_set = 0
    eff.cn0_set = float(a.p("cn0", 5.0))
    eff.force_fix = "NO_FIX"


APPLY = {"spoofing": _spoofing, "jamming": _jamming, "low_satellites": _low_satellites,
         "multipath": _multipath, "tunnel": _tunnel}


def random_anomalies(n, kinds, total_time, dur_range, rng, warmup=60.0, margin=30.0):
    specs: List[AnomalySpec] = []
    for _ in range(1000):
        if len(specs) >= n:
            break
        d = float(rng.uniform(*dur_range))
        hi = total_time - d - margin
        if hi <= warmup:
            continue
        st = float(rng.uniform(warmup, hi))
        if any(st < o.end + margin and o.start < st + d + margin for o in specs):
            continue
        specs.append(AnomalySpec(str(rng.choice(kinds)), st, d))
    return sorted(specs, key=lambda a: a.start)


def parse_anomaly(text: str) -> AnomalySpec:
    parts = text.split(":", 3)
    if len(parts) < 3:
        raise ValueError(f"bad anomaly spec '{text}' (expected kind:start:duration[:k=v,...])")
    kind = ANOMALY_ALIASES.get(parts[0].strip().lower(), parts[0].strip().lower())
    if kind not in ANOMALY_TYPES:
        raise ValueError(f"unknown anomaly '{kind}', choose from {ANOMALY_TYPES}")
    params: Dict[str, object] = {}
    if len(parts) == 4 and parts[3].strip():
        for kv in parts[3].split(","):
            key, val = kv.split("=", 1)
            params[key.strip()] = _auto(val.strip())
    return AnomalySpec(kind, float(parts[1]), float(parts[2]), params)


def _auto(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return float(v) if any(c in v for c in ".eE") else int(v)
    except ValueError:
        return v


# --------------------------------------------------------------------------- #
# Main generator
# --------------------------------------------------------------------------- #
def generate_track(cfg: TrackConfig, anomalies: Optional[List[AnomalySpec]] = None,
                   rng: Optional[np.random.Generator] = None):
    """Generate one track. Returns (rows, anomaly_specs). Each row is a dict."""
    rng = rng or np.random.default_rng()
    anomalies = [copy.deepcopy(a) for a in (anomalies or [])]
    ds = 1.0
    dt = 1.0 / cfg.rate_hz

    # ---- ground-truth kinematics ----
    heading0 = math.radians(cfg.start_heading_deg) if cfg.start_heading_deg is not None \
        else float(rng.uniform(0, 2 * math.pi))
    s, x, y, h, k = build_path(cfg.track_type, cfg.length_km * 1000.0, rng, ds, heading0)
    stop_idx = plan_stops(cfg, len(s), ds, rng)
    # braking envelope uses 85 % of the service deceleration so the train (which only reacts once it is
    # above the limit) can follow it without lagging and hitting the stop at speed (unphysical jerk).
    vlim = speed_limit_profile(k, ds, cfg.max_speed_kmh / 3.6, cfg.lat_accel, 0.85 * cfg.decel, stop_idx)
    ts, ss, vt, stop_s = simulate_truth(cfg, s, vlim, stop_idx, rng)
    N = len(ts)
    te = np.interp(ss, s, x)
    tn = np.interp(ss, s, y)
    th = np.degrees(np.interp(ss, s, h)) % 360.0
    t_lat, t_lon = enu_to_latlon(te, tn, cfg.start_lat, cfg.start_lon)

    # ---- reference availability (balises + stations) ----
    bal = np.arange(cfg.balise_spacing_m * 0.5, s[-1], cfg.balise_spacing_m)
    bal = np.concatenate([bal + rng.uniform(-100, 100, len(bal)), np.array(stop_s)])
    ref = np.abs(ss[:, None] - bal[None, :]).min(axis=1) <= cfg.ref_radius_m

    # ---- anomalies ----
    if cfg.n_random_anomalies > 0:
        anomalies += random_anomalies(cfg.n_random_anomalies, cfg.anomaly_kinds,
                                      ts[-1], cfg.anomaly_duration, rng)
    for a in anomalies:
        prepare_anomaly(a, rng)
    anomalies.sort(key=lambda a: a.start)

    # ---- noise processes ----
    pe, pn = ar1(N, cfg.noise_corr_s, dt, rng), ar1(N, cfg.noise_corr_s, dt, rng)
    fe, fn = ar1(N, 3.0, dt, rng), ar1(N, 3.0, dt, rng)                    # fast (multipath-like)
    sats_base = np.clip(np.round(cfg.sats_nominal + 1.2 * ar1(N, 120.0, dt, rng)
                                 + 0.5 * rng.standard_normal(N)), 6, 32)
    hdop_j = ar1(N, 60.0, dt, rng)
    cn0_ar = ar1(N, 30.0, dt, rng)

    # how on-board anomalies leak into the (independent) phone receivers
    affected = set(ANOMALY_ALIASES.get(a, a) for a in cfg.crowd_affected_by)
    ctl = {"valid_p": np.ones(N), "sig_mult": np.ones(N), "off_e": np.zeros(N), "off_n": np.zeros(N)}

    rows = []
    last_hd = float(th[0])
    last_good = None
    prev_m = None
    for i in range(N):
        t = float(ts[i])
        eff = Effects()
        labels = []
        for a in anomalies:
            if a.start <= t < a.end:
                APPLY[a.kind](a, t - a.start, eff, rng, cfg)
                labels.append(a.kind)

        # --- crowd control signals for this epoch ---
        if "tunnel" in labels and "tunnel" in affected:
            ctl["valid_p"][i] = 0.0
        if "jamming" in labels and "jamming" in affected:
            inten = min(1.0, max(0.0, (1.0 - eff.sats_mult) / 0.95))
            ctl["valid_p"][i] = min(ctl["valid_p"][i], max(0.05, 1.0 - inten))
            ctl["sig_mult"][i] *= 1.0 + 2.0 * inten
        if "multipath" in labels and "multipath" in affected:
            ctl["sig_mult"][i] *= 2.0
        if "spoofing" in labels and "spoofing" in affected:
            ctl["off_e"][i], ctl["off_n"][i] = eff.offset_e, eff.offset_n

        # satellites
        sats = eff.sats_set if eff.sats_set is not None \
            else sats_base[i] * eff.sats_mult + eff.sats_delta
        sats = int(max(0, round(sats)))

        # fix status
        if eff.force_fix:
            fix = eff.force_fix
        elif sats < 3:
            fix = "NO_FIX"
        elif sats == 3:
            fix = "2D"
        else:
            fix = cfg.base_fix

        # HDOP
        if eff.hdop_set is not None:
            hdop = eff.hdop_set * (1.0 + 0.01 * rng.standard_normal())
        else:
            if sats > 0:
                hdop = cfg.hdop_nominal * (cfg.sats_nominal / sats) ** 0.8 * (1 + 0.06 * hdop_j[i])
                if sats < 4:
                    hdop *= 1.0 + 2.0 * (4 - sats)
            else:
                hdop = 99.99
            hdop *= eff.hdop_mult
        hdop = float(min(max(hdop, 0.5), 99.99))

        # signal quality (mean C/N0)
        cn0 = (eff.cn0_set if eff.cn0_set is not None else cfg.cn0_nominal) \
            + cfg.cn0_noise * cn0_ar[i] * eff.cn0_noise_mult \
            + 0.4 * rng.standard_normal() + eff.cn0_delta
        cn0 = float(min(max(cn0, 0.0), 55.0))

        # measured position (local ENU)
        h_n = min(hdop, 10.0) / cfg.hdop_nominal
        sigma = cfg.pos_noise_m * cfg.error_scale * h_n
        me = te[i] + sigma * pe[i] + eff.extra_sigma_m * fe[i] + eff.offset_e
        mn = tn[i] + sigma * pn[i] + eff.extra_sigma_m * fn[i] + eff.offset_n

        # measured speed / heading
        sp_sigma = cfg.speed_noise_ms * cfg.error_scale * max(1.0, h_n)
        sp = max(0.0, vt[i] + sp_sigma * rng.standard_normal())
        if sp < 0.1:
            sp = 0.0
        if vt[i] < 0.5:
            hd = last_hd
        else:
            hd_sigma = cfg.heading_noise_deg * cfg.error_scale * (1.0 + 3.0 / (vt[i] + 1.0))
            hd = (th[i] + hd_sigma * rng.standard_normal()) % 360.0
        if eff.velocity_from_pos and prev_m is not None:   # spoofer also fakes consistent velocity
            dE, dN = me - prev_m[0], mn - prev_m[1]
            sp = math.hypot(dE, dN) / dt
            if sp > 0.5:
                hd = math.degrees(math.atan2(dE, dN)) % 360.0
        prev_m = (me, mn)
        last_hd = hd

        lat, lon = enu_to_latlon(me, mn, cfg.start_lat, cfg.start_lon)
        lat, lon = float(lat), float(lon)

        # behaviour when there is no fix
        if fix == "NO_FIX" and cfg.no_fix_output != "noisy":
            if cfg.no_fix_output == "hold" and last_good is not None:
                lat, lon, sp, hd = last_good
            elif cfg.no_fix_output == "nan":
                lat = lon = sp = hd = float("nan")
        else:
            last_good = (lat, lon, sp, hd)

        rows.append({
            "timestamp": (cfg.start_time + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "latitude": lat, "longitude": lon, "speed": sp, "heading": hd,
            "satellites_used": sats, "hdop": hdop, "fix_status": fix,
            "signal_quality": cn0,
            "reference_position_available": int(ref[i]),
            "anomaly_label": "+".join(sorted(set(labels))) if labels else "normal",
            "_truth_lat": float(t_lat[i]), "_truth_lon": float(t_lon[i]), "_truth_speed": float(vt[i]),
        })

    # ---- independent sensors built from the TRUE motion (unaffected by GNSS anomalies) ----
    if cfg.enable_imu:
        imu = simulate_imu(cfg, s, k, ss, vt, rng)
        for i, r in enumerate(rows):
            for key, arr in imu.items():
                r[key] = float(arr[i])
    if cfg.enable_crowd:
        crowd = simulate_crowd(cfg, s, x, y, h, ss, vt, th, ctl, rng)
        for i, r in enumerate(rows):
            for key, arr in crowd.items():
                r[key] = int(arr[i]) if key == "crowd_num_phones" else float(arr[i])
    return rows, anomalies


def write_csv(rows, path, speed_unit="ms", with_truth=False, imu=True, crowd=True):
    cols = list(COLUMNS)
    if imu:
        cols += IMU_COLUMNS
    if crowd:
        cols += CROWD_COLUMNS
    if with_truth:
        cols += TRUTH_COLUMNS
    f = SPEED_UNITS[speed_unit]

    def fmt(v, spec):
        return "" if isinstance(v, float) and math.isnan(v) else format(v, spec)

    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            line = [r["timestamp"], fmt(r["latitude"], ".7f"), fmt(r["longitude"], ".7f"),
                    fmt(r["speed"] * f, ".3f"), fmt(r["heading"], ".2f"),
                    r["satellites_used"], f"{r['hdop']:.2f}", r["fix_status"],
                    f"{r['signal_quality']:.1f}", r["reference_position_available"],
                    r["anomaly_label"]]
            if imu:
                line += [fmt(r["accel_x"], ".4f"), fmt(r["accel_y"], ".4f"), fmt(r["accel_z"], ".4f"),
                         fmt(r["gyro_z"], ".4f"), fmt(r["vib_rms"], ".4f")]
            if crowd:
                line += [fmt(r["crowd_latitude"], ".7f"), fmt(r["crowd_longitude"], ".7f"),
                         fmt(r["crowd_speed"] * f, ".3f"), fmt(r["crowd_heading"], ".2f"),
                         r["crowd_num_phones"], fmt(r["crowd_pos_std_m"], ".2f"),
                         fmt(r["crowd_accuracy_m"], ".2f")]
            if with_truth:
                line += [f"{r['_truth_lat']:.7f}", f"{r['_truth_lon']:.7f}", f"{r['_truth_speed'] * f:.3f}"]
            w.writerow(line)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Synthetic train GNSS track generator with injectable anomalies, IMU and crowdsourced GNSS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Quick examples")[1] if "Quick examples" in __doc__ else None)
    g = p.add_argument_group("output")
    g.add_argument("--output-dir", default="gnss_tracks")
    g.add_argument("--prefix", default="track")
    g.add_argument("--num-tracks", type=int, default=1)
    g.add_argument("--seed", type=int, default=None, help="reproducible runs (track i uses seed+i)")
    g.add_argument("--speed-unit", choices=list(SPEED_UNITS), default="ms")
    g.add_argument("--with-truth", action="store_true", help="append ground-truth columns")
    g.add_argument("--write-events", action="store_true", help="write <track>.events.json with injected anomalies")

    g = p.add_argument_group("track")
    g.add_argument("--track-type", choices=TRACK_TYPES + ["random"], default="mixed")
    g.add_argument("--length-km", type=float, default=20.0)
    g.add_argument("--duration-s", type=float, default=None)
    g.add_argument("--rate-hz", type=float, default=1.0)
    g.add_argument("--max-speed-kmh", type=float, default=160.0)
    g.add_argument("--stations", type=int, default=None, help="intermediate stops")
    g.add_argument("--start-lat", type=float, default=45.0)
    g.add_argument("--start-lon", type=float, default=9.0)
    g.add_argument("--start-heading", type=float, default=None)
    g.add_argument("--start-time", default="2025-01-01T08:00:00", help="ISO UTC start time")
    g.add_argument("--balise-spacing", type=float, default=1500.0, help="m between reference points")
    g.add_argument("--ref-radius", type=float, default=60.0, help="m around a reference point")

    g = p.add_argument_group("receiver error model")
    g.add_argument("--error-scale", type=float, default=1.0, help="global noise multiplier")
    g.add_argument("--pos-noise", type=float, default=1.5, help="1-sigma position error [m]")
    g.add_argument("--noise-corr", type=float, default=30.0, help="position error correlation time [s]")
    g.add_argument("--speed-noise", type=float, default=0.05, help="m/s")
    g.add_argument("--heading-noise", type=float, default=0.5, help="deg")
    g.add_argument("--sats-nominal", type=float, default=14.0)
    g.add_argument("--hdop-nominal", type=float, default=0.9)
    g.add_argument("--cn0-nominal", type=float, default=42.0)
    g.add_argument("--base-fix", choices=["2D", "3D", "DGPS"], default="3D")
    g.add_argument("--no-fix-output", choices=["hold", "nan", "noisy"], default="hold",
                   help="what position is reported during NO_FIX epochs")

    g = p.add_argument_group("IMU (accelerometer + gyro)")
    g.add_argument("--no-imu", action="store_true", help="omit the IMU columns")
    g.add_argument("--imu-error-scale", type=float, default=1.0, help="global IMU noise/bias multiplier")
    g.add_argument("--imu-noise", type=float, default=0.02, help="white accel noise [m/s^2]")
    g.add_argument("--imu-bias", type=float, default=0.02, help="constant accel bias 1-sigma [m/s^2]")
    g.add_argument("--vib-base", type=float, default=0.02, help="vibration RMS floor when moving [m/s^2]")
    g.add_argument("--vib-per-ms", type=float, default=0.0035, help="vibration RMS per m/s of speed")
    g.add_argument("--cant-max-deg", type=float, default=6.0, help="max track cant in curves [deg]")

    g = p.add_argument_group("crowdsourced phones")
    g.add_argument("--no-crowd", action="store_true", help="omit the crowdsourced columns")
    g.add_argument("--crowd-max-phones", type=int, default=40, help="simulated phone slots")
    g.add_argument("--crowd-mean-phones", type=float, default=15.0, help="typical phones on board")
    g.add_argument("--crowd-boarding-frac", type=float, default=0.3, help="passenger turnover per stop")
    g.add_argument("--train-length-m", type=float, default=200.0)
    g.add_argument("--antenna-offset-m", type=float, default=0.0,
                   help="antenna position from train centre, + = toward the front (adds a bias to the crowd mean)")
    g.add_argument("--phone-sigma-m", type=float, default=6.0, help="median per-axis phone error [m]")
    g.add_argument("--phone-corr-s", type=float, default=20.0)
    g.add_argument("--phone-dropout", type=float, default=0.10, help="per-epoch no-fix probability")
    g.add_argument("--phone-outlier-prob", type=float, default=0.02)
    g.add_argument("--phone-outlier-m", nargs=2, type=float, default=[30.0, 200.0], metavar=("MIN", "MAX"))
    g.add_argument("--crowd-aggregate", choices=CROWD_AGGREGATES, default="mean")
    g.add_argument("--crowd-affected-by", nargs="*", default=["tunnel"],
                   help="anomalies that also hit the phones (tunnel jamming multipath spoofing)")

    g = p.add_argument_group("anomalies")
    g.add_argument("--anomaly", action="append", default=[],
                   help="kind:start_s:duration_s[:k=v,...]  (repeatable)")
    g.add_argument("--random-anomalies", type=int, default=0, help="N random anomalies per track")
    g.add_argument("--anomaly-types", nargs="+", default=ANOMALY_TYPES)
    g.add_argument("--anomaly-duration", nargs=2, type=float, default=[30.0, 120.0], metavar=("MIN", "MAX"))
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    os.makedirs(args.output_dir, exist_ok=True)

    kinds = [ANOMALY_ALIASES.get(k, k) for k in args.anomaly_types]
    for k in kinds:
        if k not in ANOMALY_TYPES:
            sys.exit(f"unknown anomaly type '{k}', choose from {ANOMALY_TYPES}")
    affected = [ANOMALY_ALIASES.get(k, k) for k in args.crowd_affected_by]
    for k in affected:
        if k not in ("tunnel", "jamming", "multipath", "spoofing"):
            sys.exit(f"--crowd-affected-by: unsupported '{k}' (tunnel, jamming, multipath, spoofing)")
    manual = [parse_anomaly(a) for a in args.anomaly]
    start_time = datetime.fromisoformat(args.start_time.replace("Z", ""))
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    base = TrackConfig(
        length_km=args.length_km, duration_s=args.duration_s, rate_hz=args.rate_hz,
        start_lat=args.start_lat, start_lon=args.start_lon, start_heading_deg=args.start_heading,
        start_time=start_time, max_speed_kmh=args.max_speed_kmh, n_stations=args.stations,
        balise_spacing_m=args.balise_spacing, ref_radius_m=args.ref_radius,
        error_scale=args.error_scale, pos_noise_m=args.pos_noise, noise_corr_s=args.noise_corr,
        speed_noise_ms=args.speed_noise, heading_noise_deg=args.heading_noise,
        sats_nominal=args.sats_nominal, hdop_nominal=args.hdop_nominal, cn0_nominal=args.cn0_nominal,
        base_fix=args.base_fix, no_fix_output=args.no_fix_output,
        enable_imu=not args.no_imu, imu_error_scale=args.imu_error_scale,
        imu_noise_ms2=args.imu_noise, imu_bias_ms2=args.imu_bias,
        vib_base_ms2=args.vib_base, vib_per_ms=args.vib_per_ms, cant_max_deg=args.cant_max_deg,
        enable_crowd=not args.no_crowd, crowd_max_phones=args.crowd_max_phones,
        crowd_mean_phones=args.crowd_mean_phones, crowd_boarding_frac=args.crowd_boarding_frac,
        train_length_m=args.train_length_m, antenna_offset_m=args.antenna_offset_m,
        phone_sigma_m=args.phone_sigma_m, phone_corr_s=args.phone_corr_s,
        phone_dropout=args.phone_dropout, phone_outlier_prob=args.phone_outlier_prob,
        phone_outlier_m=tuple(args.phone_outlier_m), crowd_aggregate=args.crowd_aggregate,
        crowd_affected_by=affected,
        n_random_anomalies=args.random_anomalies, anomaly_kinds=kinds,
        anomaly_duration=tuple(args.anomaly_duration),
    )

    for i in range(args.num_tracks):
        rng = np.random.default_rng(None if args.seed is None else args.seed + i)
        ttype = str(rng.choice(TRACK_TYPES)) if args.track_type == "random" else args.track_type
        cfg = dataclasses.replace(base, track_type=ttype)
        rows, specs = generate_track(cfg, manual, rng)

        name = f"{args.prefix}_{i:03d}_{ttype}"
        path = os.path.join(args.output_dir, name + ".csv")
        write_csv(rows, path, args.speed_unit, args.with_truth, cfg.enable_imu, cfg.enable_crowd)
        if args.write_events:
            with open(os.path.join(args.output_dir, name + ".events.json"), "w") as fh:
                json.dump([{"kind": a.kind, "start_s": round(a.start, 1), "end_s": round(a.end, 1),
                            "params": a.params} for a in specs], fh, indent=2)

        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["anomaly_label"]] = counts.get(r["anomaly_label"], 0) + 1
        print(f"{path}: {len(rows)} rows | " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        t_end = (len(rows) - 1) / cfg.rate_hz
        for a in specs:
            if a.start >= t_end:
                print(f"  warning: {a.kind} at t={a.start:.0f}s starts after the track ends ({t_end:.0f}s)")


if __name__ == "__main__":
    main()
