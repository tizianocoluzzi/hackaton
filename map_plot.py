#!/usr/bin/env python3
"""
plot_track_map.py
Plot a CSV track produced by train_gnss_generator.py on an OpenStreetMap
background: the receiver position (coloured by anomaly_label) AND the
crowdsourced (average of passengers' phones) position.

Usage:
    python plot_track_map.py gnss_tracks/track_000_s_curve.csv
    python plot_track_map.py track.csv --zoom 12 --save map.png
    python plot_track_map.py track.csv --no-tiles          # offline, plain axes
    python plot_track_map.py track.csv --no-crowd          # receiver only
    python plot_track_map.py track.csv --crowd-link        # segments joining receiver and crowd fixes
"""
import argparse
import math

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt

COLORS = {
    "normal": "tab:blue",
    "spoofing": "tab:red",
    "jamming": "tab:orange",
    "low_satellites": "tab:purple",
    "multipath": "tab:green",
    "tunnel": "black",
}
CROWD_COLOR = "magenta"


def auto_zoom(extent):
    span = max(extent[1] - extent[0], extent[3] - extent[2], 1e-4)
    return int(np.clip(math.log2(360.0 * 5 / span), 3, 16))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--zoom", type=int, default=None, help="OSM tile zoom (default: auto)")
    ap.add_argument("--save", default=None, help="save figure to this path instead of only showing it")
    ap.add_argument("--no-tiles", action="store_true", help="skip OSM tiles (offline use)")
    ap.add_argument("--no-truth", action="store_true", help="ignore truth_* columns if present")
    ap.add_argument("--no-crowd", action="store_true", help="ignore crowd_* columns if present")
    ap.add_argument("--crowd-link", action="store_true",
                    help="draw a thin line between each receiver fix and the crowd fix of the same epoch")
    ap.add_argument("--marker-size", type=float, default=12)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    df = df.dropna(subset=["latitude", "longitude"]).reset_index(drop=True)
    lat, lon = df["latitude"].to_numpy(), df["longitude"].to_numpy()

    has_truth = {"truth_latitude", "truth_longitude"} <= set(df.columns) and not args.no_truth
    has_crowd = {"crowd_latitude", "crowd_longitude"} <= set(df.columns) and not args.no_crowd

    # crowd epochs with a valid fix (empty during tunnels / no phones)
    if has_crowd:
        cm = df["crowd_latitude"].notna() & df["crowd_longitude"].notna()
        clat = df.loc[cm, "crowd_latitude"].to_numpy()
        clon = df.loc[cm, "crowd_longitude"].to_numpy()
        has_crowd = len(clat) > 0

    all_lat, all_lon = [lat], [lon]
    if has_truth:
        all_lat.append(df["truth_latitude"].to_numpy()); all_lon.append(df["truth_longitude"].to_numpy())
    if has_crowd:
        all_lat.append(clat); all_lon.append(clon)
    all_lat, all_lon = np.concatenate(all_lat), np.concatenate(all_lon)

    # Bounds: (lon_min, lon_max, lat_min, lat_max), padded by 10 %
    pad_lon = 0.1 * max(np.ptp(all_lon), 1e-3)
    pad_lat = 0.1 * max(np.ptp(all_lat), 1e-3)
    extent = [all_lon.min() - pad_lon, all_lon.max() + pad_lon,
              all_lat.min() - pad_lat, all_lat.max() + pad_lat]

    if args.no_tiles:
        crs = ccrs.PlateCarree()
        request = None
    else:
        request = cimgt.OSM()
        crs = request.crs

    fig = plt.figure(figsize=(11, 8))
    ax = plt.axes(projection=crs)
    ax.set_extent(extent, crs=ccrs.PlateCarree())
    if request is not None:
        ax.add_image(request, args.zoom or auto_zoom(extent))
    else:
        ax.gridlines(draw_labels=True)

    pc = ccrs.PlateCarree()

    # Ground truth (if the CSV was generated with --with-truth)
    if has_truth:
        ax.plot(df["truth_longitude"], df["truth_latitude"], transform=pc,
                color="gray", linewidth=1.5, linestyle="--", zorder=8, label="truth")

    # Crowdsourced position (average of the passengers' phones)
    if has_crowd:
        if args.crowd_link:
            cl = df.loc[cm]
            for a_lon, a_lat, b_lon, b_lat in zip(cl["longitude"], cl["latitude"],
                                                  cl["crowd_longitude"], cl["crowd_latitude"]):
                ax.plot([a_lon, b_lon], [a_lat, b_lat], transform=pc, color=CROWD_COLOR,
                        linewidth=0.4, alpha=0.4, zorder=8)
        ax.plot(clon, clat, transform=pc, color=CROWD_COLOR, linewidth=1.0, alpha=0.5, zorder=9)
        ax.scatter(clon, clat, transform=pc, s=args.marker_size * 0.8, marker="D", facecolor="none",
                   edgecolor=CROWD_COLOR, linewidth=0.8, zorder=10, label="crowd (mean of phones)")

    # Measured receiver track
    ax.plot(lon, lat, transform=pc, color="tab:blue", linewidth=1.5, alpha=0.6, zorder=9)

    # Measured receiver points, coloured by anomaly label
    labels = df["anomaly_label"].fillna("normal").astype(str)
    for name in sorted(labels.unique()):
        m = (labels == name).to_numpy()
        color = COLORS.get(name.split("+")[0], "tab:pink")
        ax.scatter(lon[m], lat[m], transform=pc, s=args.marker_size, color=color,
                   zorder=11, label=f"receiver: {name}")

    # Start / end markers
    ax.scatter(lon[0], lat[0], transform=pc, marker="^", s=120, color="lime",
               edgecolor="k", zorder=12, label="start")
    ax.scatter(lon[-1], lat[-1], transform=pc, marker="s", s=100, color="gold",
               edgecolor="k", zorder=12, label="end")

    ax.legend(loc="upper right")
    ax.set_title(f"{args.csv}  ({len(df)} points)")
    plt.tight_layout()

    if args.save:
        fig.savefig(args.save, dpi=150)
        print("saved", args.save)
    plt.show()


if __name__ == "__main__":
    main()
