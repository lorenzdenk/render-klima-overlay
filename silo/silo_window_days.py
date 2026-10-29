#!/usr/bin/env python3
"""
silo_window_days.py - Zaehlt pro Rasterzelle die Tage eines festen Kalenderfensters,
an denen eine SILO-Tagesvariable eine Schwelle erfuellt, und mittelt ueber Jahre.
Ergebnis: ESRI-ASCII-Grid (EPSG:4326), direkt fuer render_mbtiles.py.

Standard: min_temp <= 10, 20.11.-07.12., Jahre 1991-2020, Tasmanien.

Voraussetzungen:  pip install numpy xarray netcdf4

SILO-Jahresdateien liegen auf S3 als  annual/<folder>/<jahr>.<stem>.nc
(meist ist folder == stem == Variablenname; Ausnahme: folder cloud_oktas_radiation
enthaelt Dateien <jahr>.radiation.nc, daher --folder und --stem getrennt).

Beispiele:
  python silo_window_days.py --var min_temp --op le --threshold 10 --out tmin.asc
  python silo_window_days.py --var daily_rain --op ge --threshold 1 --out regen.asc
  python silo_window_days.py --folder cloud_oktas_radiation --stem radiation \
      --var cloud_oktas_radiation --op le --threshold 3 --expect-range 0 8 --out wolken.asc

Lokale Dateien: --dir <ordner> erwartet <ordner>/<folder>/<jahr>.<stem>.nc;
fehlende Dateien werden von S3 geladen und (ohne --keep) nach der Auswertung geloescht.
"""
import argparse
import os
import sys
import urllib.request

import numpy as np
import xarray as xr

BASE = "https://s3-ap-southeast-2.amazonaws.com/silo-open-data/Official/annual/"
TASMANIA = (143.5, -43.8, 148.6, -39.5)  # lon_min, lat_min, lon_max, lat_max (inkl. King/Flinders Island)
_announced = set()


def parse_years(s):
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def md(s):
    m, d = s.split("-")
    return int(m), int(d)


def find_dim(ds, key):
    for d in ds.dims:
        if key in str(d).lower():
            return d
    sys.exit(f"Keine Dimension mit '{key}' gefunden: {list(ds.dims)}")


def fetch(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=180) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(tmp, dest)


def count_year(path, var, bbox, start, end, thr, op, expect):
    with xr.open_dataset(path) as ds:
        v = var if var in ds.data_vars else next(
            (n for n, da in ds.data_vars.items() if da.ndim == 3), None)
        if v is None:
            sys.exit(f"Keine 3D-Variable in {path}: {list(ds.data_vars)}")
        da = ds[v]
        if path not in _announced and len(_announced) == 0:
            _announced.add(path)
            print(f"  Variablen in der Datei: {list(ds.data_vars)}  -> verwendet: {v!r}")
            print(f"  long_name={da.attrs.get('long_name')!r} units={da.attrs.get('units')!r}")
        tdim, ydim, xdim = find_dim(ds, "time"), find_dim(ds, "lat"), find_dim(ds, "lon")
        da = da.sortby(ydim, ascending=False)  # Norden zuerst
        lon_min, lat_min, lon_max, lat_max = bbox
        da = da.sel({ydim: slice(lat_max, lat_min), xdim: slice(lon_min, lon_max)})
        t = da[tdim]
        key = t.dt.month * 100 + t.dt.day
        k0, k1 = start[0] * 100 + start[1], end[0] * 100 + end[1]
        sel = (key >= k0) & (key <= k1) if k0 <= k1 else (key >= k0) | (key <= k1)
        win = da.isel({tdim: np.where(sel.values)[0]}).load()
        ndays = win.sizes[tdim]
        if ndays == 0:
            raise RuntimeError("Fenster enthaelt keine Tage")
        vals = win.values.astype(np.float32)
        valid = np.isfinite(vals)
        if not valid.any():
            raise RuntimeError("Keine gueltigen Werte im Ausschnitt (Gebiet/Gitter pruefen)")
        lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
        if expect and (lo < expect[0] - 1e-6 or hi > expect[1] + 1e-6):
            sys.exit(f"Wertebereich {lo:.2f}..{hi:.2f} ausserhalb der Erwartung {expect[0]}..{expect[1]} "
                     f"(Variable {v!r}) - vermutlich die falsche Variable/Datei. Abbruch.")
        hit = (vals <= thr) if op == "le" else (vals >= thr)
        cnt = (hit & valid).sum(axis=0).astype(np.float32)
        cnt[~valid.any(axis=0)] = np.nan
        return cnt, win[ydim].values, win[xdim].values, ndays, (lo, hi)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--years", default="1991-2020", help="z. B. 1991-2020 oder 2019,2020")
    p.add_argument("--start", default="11-20", help="MM-TT")
    p.add_argument("--end", default="12-07", help="MM-TT")
    p.add_argument("--var", default="min_temp", help="Variablenname in der NetCDF-Datei")
    p.add_argument("--folder", help="S3-Ordner (Standard: --var)")
    p.add_argument("--stem", help="Dateiname vor .nc ohne Jahr (Standard: --folder)")
    p.add_argument("--threshold", type=float, default=10.0)
    p.add_argument("--op", choices=["le", "ge"], default="le", help="le: <= Schwelle, ge: >= Schwelle")
    p.add_argument("--expect-range", type=float, nargs=2, metavar=("LO", "HI"),
                   help="Abbruch, wenn Werte ausserhalb liegen (Schutz vor falscher Datei)")
    p.add_argument("--bbox", type=float, nargs=4, metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"), default=TASMANIA)
    p.add_argument("--dir", help="Basisordner fuer lokale Dateien/Download")
    p.add_argument("--keep", action="store_true", help="heruntergeladene Dateien behalten")
    p.add_argument("--stat", choices=["mean", "median"], default="mean", help="Aggregation ueber die Jahre")
    p.add_argument("--out", required=True, help="Ausgabe .asc")
    a = p.parse_args()

    folder = a.folder or a.var
    stem = a.stem or folder
    years = parse_years(a.years)
    start, end = md(a.start), md(a.end)
    workdir = a.dir or os.path.join(os.path.dirname(os.path.abspath(a.out)), "_silo_tmp")

    stack, lat, lon, used = [], None, None, []
    nd = 0
    for y in years:
        path = os.path.join(workdir, folder, f"{y}.{stem}.nc")
        downloaded = False
        if not os.path.exists(path):
            url = f"{BASE}{folder}/{y}.{stem}.nc"
            print(f"{y}: lade {url}", flush=True)
            try:
                fetch(url, path)
                downloaded = True
            except Exception as e:
                print(f"  uebersprungen ({e})")
                continue
        try:
            cnt, la, lo, nd, rng = count_year(path, a.var, a.bbox, start, end, a.threshold, a.op, a.expect_range)
        finally:
            if downloaded and not a.keep and os.path.exists(path):
                os.remove(path)
        if lat is None:
            lat, lon = la, lo
        elif cnt.shape != stack[0].shape:
            sys.exit(f"{y}: Gitter weicht ab")
        stack.append(cnt)
        used.append(y)
        print(f"{y}: {nd} Tage im Fenster, Werte {rng[0]:.1f}..{rng[1]:.1f}, "
              f"Mittel der Zaehlung ueber Zellen {np.nanmean(cnt):.2f}", flush=True)

    if not stack:
        sys.exit("Keine Jahre verarbeitet.")
    arr = np.stack(stack)
    res = np.nanmean(arr, axis=0) if a.stat == "mean" else np.nanmedian(arr, axis=0)
    print(f"{len(used)} Jahre ({used[0]}-{used[-1]}), Ergebnis min {np.nanmin(res):.2f} "
          f"mittel {np.nanmean(res):.2f} max {np.nanmax(res):.2f} (Fenstertage: {nd})")

    cs = float(abs(lon[1] - lon[0]))
    xll, yll = float(lon.min() - cs / 2), float(lat.min() - cs / 2)
    out = np.where(np.isfinite(res), np.round(res, 3), -999.0)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write(f"NCOLS {out.shape[1]}\nNROWS {out.shape[0]}\nXLLCORNER {xll:.6f}\nYLLCORNER {yll:.6f}\n"
                f"CELLSIZE {cs:.6f}\nNODATA_VALUE -999\n")
        for row in out:
            f.write(" ".join(f"{v:g}" for v in row) + "\n")
    print(f"geschrieben: {a.out}  ({out.shape[1]} x {out.shape[0]} Zellen, {cs:g} Grad)")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    main()
