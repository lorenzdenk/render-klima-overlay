#!/usr/bin/env python3
"""
combine_months.py - Verknuepft mehrere Monatsraster (GeoTIFF, gleiches Gitter) zu einem Raster.

  --op mean   gewichtetes Mittel   sum(w_i * x_i) / sum(w_i)      (z. B. Tmin)
  --op sum    gewichtete Summe     sum(w_i * x_i)                 (z. B. Niederschlag in mm im Fenster)

Beispiele (Fenster 20.11.-07.12. = 11 Tage November, 7 Tage Dezember):
  # einfaches Mittel Nov/Dez
  combine_months.py tmin_nov.tif tmin_dec.tif -o tmin_novdec.tif --op mean
  # Tmin gewichtet nach Tagen im Fenster
  combine_months.py tmin_nov.tif tmin_dec.tif -o tmin_fenster.tif --op mean --weights 11 7
  # Niederschlag im Fenster in mm: 11/30 des November + 7/31 des Dezember
  combine_months.py rain_nov.tif rain_dec.tif -o rain_fenster.tif --op sum --weights 0.36667 0.22581
  # mittlere Monatssumme Nov/Dez in mm
  combine_months.py rain_nov.tif rain_dec.tif -o rain_novdec.tif --op mean

Fehlt in einem Raster ein Wert (NoData), ist das Ergebnis dort NoData.
Verarbeitet blockweise, braucht also wenig Arbeitsspeicher.
"""
import argparse
import sys

import numpy as np
import rasterio


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--op", choices=["mean", "sum"], default="mean")
    p.add_argument("--weights", type=float, nargs="+")
    a = p.parse_args()
    if len(a.inputs) < 2:
        sys.exit("Mindestens zwei Eingaben.")
    w = a.weights or [1.0] * len(a.inputs)
    if len(w) != len(a.inputs):
        sys.exit("Anzahl --weights muss zur Anzahl Eingaben passen.")

    srcs = [rasterio.open(f) for f in a.inputs]
    ref = srcs[0]
    for f, s in zip(a.inputs[1:], srcs[1:]):
        if (s.width, s.height) != (ref.width, ref.height) or s.transform != ref.transform or s.crs != ref.crs:
            sys.exit(f"{f}: Gitter/CRS weicht von {a.inputs[0]} ab "
                     f"({s.width}x{s.height}, {s.crs} vs {ref.width}x{ref.height}, {ref.crs}). "
                     "Bitte mit gleichem Ausschnitt laden (gleiche WCS-Parameter).")
    prof = ref.profile.copy()
    prof.update(count=1, dtype="float32", nodata=np.nan, compress="deflate", predictor=3,
                tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
    n_valid = 0
    vmin, vmax, vsum = np.inf, -np.inf, 0.0
    with rasterio.open(a.output, "w", **prof) as dst:
        for _, win in dst.block_windows(1):
            acc = np.zeros((win.height, win.width), np.float64)
            bad = np.zeros((win.height, win.width), bool)
            for s, wi in zip(srcs, w):
                m = s.read(1, window=win, masked=True)
                bad |= np.ma.getmaskarray(m) | ~np.isfinite(m.filled(np.nan).astype("float64"))
                acc += wi * m.filled(0).astype("float64")
            if a.op == "mean":
                acc /= sum(w)
            acc[bad] = np.nan
            dst.write(acc.astype("float32"), 1, window=win)
            ok = np.isfinite(acc)
            if ok.any():
                n_valid += int(ok.sum()); vmin = min(vmin, float(acc[ok].min()))
                vmax = max(vmax, float(acc[ok].max())); vsum += float(acc[ok].sum())
    print(f"{a.output}: {ref.width} x {ref.height} Zellen, CRS {ref.crs}, gueltig {n_valid}, "
          f"min {vmin:.2f} mittel {vsum/max(n_valid,1):.2f} max {vmax:.2f}")


if __name__ == "__main__":
    main()
