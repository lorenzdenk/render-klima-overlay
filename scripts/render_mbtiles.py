#!/usr/bin/env python3
"""
render_mbtiles.py - Rasterdaten (ESRI-ASCII oder GeoTIFF) als eingefaerbtes
WebP-MBTiles-Overlay (Web-Mercator, XYZ/TMS) rendern.

Wichtig:
  * Hoechste Zoomstufe = Zoomstufe, bei der ein Kachelpixel so gross ist wie die
    Datenzelle (Breitengrad-korrigiert), plus --extra-zoom (Standard 1, damit die
    Zellen als scharfe Bloecke erscheinen). Mehr wird nie gerendert.
  * Niedrigste Zoomstufe = Stufe, auf der die Daten etwa in eine Kachel passen
    (oder --minzoom).
  * Kachelbereich = nur Kacheln, die gueltige Daten enthalten (leere werden
    uebersprungen). Bereich und Zoom ergeben sich aus den Eingabedaten.
  * Werte werden VOR dem Einfaerben resampelt (nie Farben mischen).
    Niedrige Zoomstufen nutzen eine Mittelwert-Pyramide gegen Aliasing.

Beispiele:
  render_mbtiles.py snow.asc.gz -o snow.mbtiles --crs EPSG:31467 \
      --cmap Blues --vmin 0 --vmax 150 --gamma 0.6 --unit "Tage/Jahr"
  render_mbtiles.py snow.asc.gz -o lk_kn.mbtiles --crs EPSG:31467 \
      --mask kreise.geo.json --mask-filter NAME_3=Konstanz
  render_mbtiles.py --jobs jobs.json
"""
import argparse
import gzip
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
import time
import warnings
from multiprocessing import Pool

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer

R = 6378137.0
ORIGIN = math.pi * R
WORLD = 2 * ORIGIN


# --------------------------------------------------------------------------
# Eingabe
# --------------------------------------------------------------------------
def load_asc(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        hdr = {}
        for _ in range(6):
            parts = f.readline().split()
            hdr[parts[0].lower()] = float(parts[1])
        data = np.fromstring(f.read(), sep=" ", dtype=np.float32)
    nc, nr, cs = int(hdr["ncols"]), int(hdr["nrows"]), hdr["cellsize"]
    if data.size != nc * nr:
        raise ValueError(f"{path}: {data.size} Werte, erwartet {nc*nr}")
    arr = data.reshape(nr, nc)
    nod = hdr.get("nodata_value", -9999.0)
    arr[arr == nod] = np.nan
    if "xllcenter" in hdr:
        xl, yl = hdr["xllcenter"] - cs / 2, hdr["yllcenter"] - cs / 2
    else:
        xl, yl = hdr["xllcorner"], hdr["yllcorner"]
    return arr, xl, yl + nr * cs, cs, None


def load_tif(path):
    try:
        import rasterio
    except ImportError:
        sys.exit("GeoTIFF braucht rasterio (pip install rasterio).")
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True).astype("float32").filled(np.nan)
        t = src.transform
        if abs(t.b) > 0 or abs(t.d) > 0:
            sys.exit("Gedrehte Rasters werden nicht unterstuetzt.")
        if abs(abs(t.a) - abs(t.e)) > 1e-6 * abs(t.a):
            sys.exit("Nicht-quadratische Zellen werden nicht unterstuetzt.")
        crs = src.crs.to_string() if src.crs else None
        return arr, t.c, t.f, abs(t.a), crs


def load_grid(path, crs_arg):
    low = path.lower()
    if low.endswith((".asc", ".asc.gz")):
        arr, xl, yt, cs, crs = load_asc(path)
    elif low.endswith((".tif", ".tiff")):
        arr, xl, yt, cs, crs = load_tif(path)
    else:
        sys.exit(f"Unbekanntes Format: {path}")
    crs = crs_arg or crs
    if not crs:
        sys.exit("Kein CRS bekannt - bitte --crs angeben (z. B. EPSG:31467).")
    return arr, xl, yt, cs, CRS.from_user_input(crs)


# --------------------------------------------------------------------------
# Grid / Pyramide
# --------------------------------------------------------------------------
def reduce2(a):
    h, w = a.shape
    a = np.pad(a, ((0, h % 2), (0, w % 2)), constant_values=np.nan)
    b = a.reshape(a.shape[0] // 2, 2, a.shape[1] // 2, 2)
    cnt = np.isfinite(b).sum(axis=(1, 3))
    s = np.nansum(b, axis=(1, 3))
    return np.where(cnt >= 2, s / np.maximum(cnt, 1), np.nan).astype(np.float32)


def build_pyramid(arr, maxlevels=12):
    levels = [arr]
    while min(levels[-1].shape) >= 4 and len(levels) < maxlevels:
        levels.append(reduce2(levels[-1]))
    return levels


def apply_mask(arr, x_left, y_top, cs, crs, geojson, filters):
    import shapely
    from shapely.geometry import shape
    from shapely.ops import unary_union

    with open(geojson) as f:
        gj = json.load(f)
    feats = gj["features"] if "features" in gj else [gj]
    sel = []
    for ft in feats:
        props = ft.get("properties", {})
        if all(str(props.get(k)) == v for k, v in filters):
            sel.append(shape(ft["geometry"]))
    if not sel:
        sys.exit(f"Maske: kein Feature passt auf {filters}")
    geom = unary_union(sel)
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    geom = shapely.transform(geom, lambda c: np.column_stack(tr.transform(c[:, 0], c[:, 1])))
    minx, miny, maxx, maxy = geom.bounds
    nr, nc = arr.shape
    c0 = max(0, int((minx - x_left) // cs) - 1)
    c1 = min(nc, int((maxx - x_left) // cs) + 2)
    r0 = max(0, int((y_top - maxy) // cs) - 1)
    r1 = min(nr, int((y_top - miny) // cs) + 2)
    xs = x_left + (np.arange(c0, c1) + 0.5) * cs
    ys = y_top - (np.arange(r0, r1) + 0.5) * cs
    X, Y = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(geom, X, Y)
    out = np.full_like(arr, np.nan)
    sub = arr[r0:r1, c0:c1]
    out[r0:r1, c0:c1] = np.where(inside, sub, np.nan)
    return out


# --------------------------------------------------------------------------
# Kachel-Rendering (laeuft in Worker-Prozessen)
# --------------------------------------------------------------------------
S = {}  # globaler Zustand, vor dem Fork gesetzt


def _init_worker(state):
    S.update(state)


def _tr():
    if "tr" not in S:
        S["tr"] = Transformer.from_crs("EPSG:3857", S["crs"], always_xy=True)
    return S["tr"]


def _get(a, r, c):
    ok = (r >= 0) & (r < a.shape[0]) & (c >= 0) & (c < a.shape[1])
    v = np.full(r.shape, np.nan, np.float32)
    v[ok] = a[r[ok], c[ok]]
    return v


def sample(a, csL, x_left, y_top, sx, sy, mode):
    fc = (sx - x_left) / csL
    fr = (y_top - sy) / csL
    nearest = _get(a, np.floor(fr).astype(np.int64), np.floor(fc).astype(np.int64))
    if mode == "nearest":
        return nearest
    c0f = np.floor(fc - 0.5)
    r0f = np.floor(fr - 0.5)
    tc = (fc - 0.5 - c0f).astype(np.float32)
    trw = (fr - 0.5 - r0f).astype(np.float32)
    c0, r0 = c0f.astype(np.int64), r0f.astype(np.int64)
    acc = np.zeros(sx.shape, np.float32)
    wsum = np.zeros(sx.shape, np.float32)
    for dr, wr in ((0, 1 - trw), (1, trw)):
        for dc, wc in ((0, 1 - tc), (1, tc)):
            v = _get(a, r0 + dr, c0 + dc)
            w = wr * wc * np.isfinite(v)
            acc += np.where(np.isfinite(v), v, 0) * w
            wsum += w
    out = np.where(wsum > 0, acc / np.maximum(wsum, 1e-6), np.nan).astype(np.float32)
    out[~np.isfinite(nearest)] = np.nan  # Umriss bleibt scharf
    return out


def _font(px):
    key = ("font", px)
    if key not in S:
        from PIL import ImageFont
        f = None
        for path in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "arialbd.ttf", "Arial Bold.ttf"):
            try:
                f = ImageFont.truetype(path, px)
                break
            except OSError:
                continue
        S[key] = f or ImageFont.load_default(size=px)
    return S[key]


def draw_label(im, value, ts, P):
    """Mittelwert der Kachel (gueltige Pixel) als kleine Zahl in die Kachelmitte, dunkel mit hellem Rand."""
    from PIL import ImageDraw
    txt = f"{value:.{P['label_decimals']}f}".replace(".", P["label_sep"]) + P["label_suffix"]
    d = ImageDraw.Draw(im)
    d.text((ts / 2, ts / 2), txt, font=_font(P["label_px"]), anchor="mm", fill=(20, 20, 20, 255),
           stroke_width=max(1, P["label_px"] // 7), stroke_fill=(255, 255, 255, 230))


def render_tile(job):
    z, x, y = job
    P = S["p"]
    ts = P["tile_size"]
    res = WORLD / (ts * 2 ** z)
    px = x * ts * res - ORIGIN + (np.arange(ts) + 0.5) * res
    py = ORIGIN - y * ts * res - (np.arange(ts) + 0.5) * res
    X, Y = np.meshgrid(px, py)
    sx, sy = _tr().transform(X, Y)
    bad = ~(np.isfinite(sx) & np.isfinite(sy))  # ausserhalb des Gueltigkeitsbereichs der Projektion
    if bad.any():
        sx = np.where(bad, -1e15, sx)
        sy = np.where(bad, -1e15, sy)
    lat_c = math.degrees(math.atan(math.sinh((ORIGIN - (y + 0.5) * ts * res) / R)))
    ground = res * math.cos(math.radians(lat_c))
    lv = int(np.clip(math.floor(math.log2(max(ground / P["cs_m"], 1e-9))), 0, len(S["levels"]) - 1))
    v = sample(S["levels"][lv], P["cs"] * 2 ** lv, P["x_left"], P["y_top"], sx, sy, P["resample"])
    fin = np.isfinite(v)
    if not fin.any():
        return z, x, y, None
    n = np.clip((np.where(fin, v, P["vmin"]) - P["vmin"]) / (P["vmax"] - P["vmin"]), 0, 1)
    if P["gamma"] != 1.0:
        n = n ** P["gamma"]
    rgba = S["lut"][(n * 255 + 0.5).astype(np.uint8)].copy()
    rgba[..., 3] = np.where(fin, rgba[..., 3], 0)
    buf = io.BytesIO()
    im = Image.fromarray(rgba, "RGBA")
    if P.get("label_zoom") is not None and z == P["label_zoom"] and fin.mean() >= P["label_min_cover"]:
        draw_label(im, float(np.mean(v[fin])), ts, P)
    if P["webp_quality"] is None:
        im.save(buf, "WEBP", lossless=True, method=4)
    else:
        im.save(buf, "WEBP", quality=P["webp_quality"], method=4)
    return z, x, y, buf.getvalue()


# --------------------------------------------------------------------------
# Hauptablauf
# --------------------------------------------------------------------------
def run(a):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t0 = time.time()
    arr, x_left, y_top, cs, crs = load_grid(a.input, a.crs)
    arr = arr * np.float32(a.scale)
    if a.mask:
        filters = [tuple(f.split("=", 1)) for f in (a.mask_filter or [])]
        arr = apply_mask(arr, x_left, y_top, cs, crs, a.mask, filters)
    nr, nc = arr.shape
    fin = np.isfinite(arr)
    if not fin.any():
        sys.exit("Keine gueltigen Daten.")

    rows = np.where(fin.any(axis=1))[0]
    cols = np.where(fin.any(axis=0))[0]
    xmin, xmax = x_left + cols[0] * cs, x_left + (cols[-1] + 1) * cs
    ymax, ymin = y_top - rows[0] * cs, y_top - (rows[-1] + 1) * cs

    # Bounding Box (Kanten verdichtet) nach 3857 und 4326
    t = np.linspace(0, 1, 41)
    ex = np.concatenate([xmin + t * (xmax - xmin), np.full(41, xmax), xmax - t * (xmax - xmin), np.full(41, xmin)])
    ey = np.concatenate([np.full(41, ymin), ymin + t * (ymax - ymin), np.full(41, ymax), ymax - t * (ymax - ymin)])
    mx, my = Transformer.from_crs(crs, "EPSG:3857", always_xy=True).transform(ex, ey)
    lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(ex, ey)
    lat_c = float((lat.min() + lat.max()) / 2)
    bounds = [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]

    cs_m = cs * 111320.0 * math.cos(math.radians(lat_c)) if crs.is_geographic else cs
    zfull = WORLD * math.cos(math.radians(lat_c)) / a.tile_size  # Bodenmeter/Pixel bei z0
    z_native = max(0, math.ceil(math.log2(zfull / cs_m)))
    maxzoom = min(22, z_native + a.extra_zoom)
    if a.maxzoom is not None:  # Obergrenze, nie ueber die Datenaufloesung (+extra) hinaus
        maxzoom = min(maxzoom, a.maxzoom)
    extent = max(mx.max() - mx.min(), my.max() - my.min()) * math.cos(math.radians(lat_c))
    auto_min = max(0, math.floor(math.log2(WORLD * math.cos(math.radians(lat_c)) / extent)))
    # Standard: 3 Stufen unter "Daten passen in eine Kachel", damit das Overlay
    # beim Herauszoomen nicht sofort verschwindet (dort 1 Kachel pro Stufe).
    minzoom = max(0, auto_min - 3) if a.minzoom is None else a.minzoom
    minzoom = min(minzoom, maxzoom)

    vals = arr[fin]
    vmin = a.vmin if a.vmin is not None else float(np.percentile(vals, 2))
    vmax = a.vmax if a.vmax is not None else float(np.percentile(vals, 98))
    if vmax <= vmin:
        sys.exit("vmax muss groesser als vmin sein.")

    print(f"Eingabe  : {a.input}")
    print(f"Raster   : {nc} x {nr} Zellen, {cs:g} {'deg' if crs.is_geographic else 'm'}, {crs.to_string()}")
    print(f"Werte    : min {vals.min():.2f}  mittel {vals.mean():.2f}  max {vals.max():.2f}  (gueltig: {fin.sum()})")
    print(f"Skala    : {a.cmap} {vmin:g}..{vmax:g} gamma {a.gamma:g}")
    print(f"Bereich  : lon {bounds[0]:.3f}..{bounds[2]:.3f}  lat {bounds[1]:.3f}..{bounds[3]:.3f}")
    print(f"Zoom     : {minzoom}..{maxzoom}  (nativ {z_native} bei {lat_c:.1f} Grad, +{a.extra_zoom} extra)")

    cm = plt.get_cmap(a.cmap)
    lut = (cm(np.linspace(0, 1, 256)) * 255 + 0.5).astype(np.uint8)
    lut[:, 3] = int(round(255 * a.opacity))

    S.clear()
    S.update(
        crs=crs, levels=build_pyramid(arr), lut=lut,
        p=dict(tile_size=a.tile_size, cs=cs, cs_m=cs_m, x_left=x_left, y_top=y_top,
               vmin=vmin, vmax=vmax, gamma=a.gamma, resample=a.resample,
               webp_quality=a.webp_quality,
               label_zoom=(maxzoom if a.label_maxzoom else None), label_px=a.label_px,
               label_decimals=a.label_decimals, label_suffix=a.label_suffix, label_sep=a.label_sep,
               label_min_cover=a.label_min_cover),
    )

    jobs = []
    for z in range(minzoom, maxzoom + 1):
        tm = WORLD / 2 ** z
        n = 2 ** z
        x0 = max(0, int((mx.min() + ORIGIN) // tm))
        x1 = min(n - 1, int((mx.max() + ORIGIN) // tm))
        y0 = max(0, int((ORIGIN - my.max()) // tm))
        y1 = min(n - 1, int((ORIGIN - my.min()) // tm))
        jobs += [(z, xx, yy) for xx in range(x0, x1 + 1) for yy in range(y0, y1 + 1)]
    print(f"Kandidaten-Kacheln: {len(jobs)}")

    out = a.output
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    # Deduplizierte MBTiles (map + images, View tiles) wie lorimaps-render / render-baumhoehen-overlay:
    # gleiche Kacheln (z. B. einfarbige Flaechen) werden nur einmal gespeichert.
    db.executescript("""
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_id TEXT);
        CREATE TABLE images (tile_id TEXT, tile_data BLOB);
        CREATE UNIQUE INDEX images_id ON images (tile_id);
        CREATE UNIQUE INDEX map_index ON map (zoom_level, tile_column, tile_row);
        CREATE VIEW tiles AS SELECT map.zoom_level AS zoom_level, map.tile_column AS tile_column,
            map.tile_row AS tile_row, images.tile_data AS tile_data FROM map JOIN images ON images.tile_id = map.tile_id;
    """)
    n_unique = 0

    per_zoom = {}
    workers = max(1, a.workers)
    pool = None
    if workers == 1:
        it = map(render_tile, jobs)
    else:
        # Zustand explizit an die Worker uebergeben (Windows/macOS starten per "spawn", nicht "fork")
        pool = Pool(workers, initializer=_init_worker, initargs=({k: v for k, v in S.items() if k != "tr"},))
        it = pool.imap_unordered(render_tile, jobs, chunksize=4)
    for z, x, y, data in it:
        if data is None:
            continue
        h = hashlib.md5(data).hexdigest()
        n_unique += db.execute("INSERT OR IGNORE INTO images VALUES (?,?)", (h, data)).rowcount
        db.execute("INSERT OR REPLACE INTO map VALUES (?,?,?,?)", (z, x, (1 << z) - 1 - y, h))
        c = per_zoom.setdefault(z, [0, 0])
        c[0] += 1
        c[1] += len(data)
    if pool is not None:
        pool.close()
        pool.join()
    unit =f" [{a.unit}]" if a.unit else ""
    meta = {
        "name": a.name or os.path.splitext(os.path.basename(out))[0],
        "format": "webp",
        "bounds": ",".join(f"{b:.6f}" for b in bounds),
        "center": f"{(bounds[0]+bounds[2])/2:.6f},{(bounds[1]+bounds[3])/2:.6f},{minzoom}",
        "minzoom": str(minzoom), "maxzoom": str(maxzoom),
        "type": "overlay", "version": "1",
        "description": a.description or f"{a.name or ''}{unit}; {a.cmap} {vmin:g}..{vmax:g}",
        "attribution": a.attribution or "",
        "tile_size": str(a.tile_size),
    }
    if a.tile_size == 512:
        meta["scale"] = "2"
    db.executemany("INSERT INTO metadata VALUES (?,?)", list(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()

    # Legende + Sidecar
    base = os.path.splitext(out)[0]
    fig, ax = plt.subplots(figsize=(4.2, 0.8), dpi=200)
    from matplotlib.colors import PowerNorm
    sm = plt.cm.ScalarMappable(norm=PowerNorm(a.gamma, vmin, vmax, clip=True) if a.gamma != 1 else plt.Normalize(vmin, vmax), cmap=cm)
    cb = fig.colorbar(sm, cax=ax, orientation="horizontal")
    cb.set_label((a.name or "") + unit, fontsize=6)
    cb.ax.tick_params(labelsize=6)
    fig.savefig(base + ".legend.png", bbox_inches="tight")
    plt.close(fig)
    side = dict(meta, vmin=vmin, vmax=vmax, gamma=a.gamma, cmap=a.cmap, unit=a.unit,
                native_zoom=z_native, extra_zoom=a.extra_zoom, input=os.path.basename(a.input))
    with open(base + ".meta.json", "w") as f:
        json.dump(side, f, indent=2, ensure_ascii=False)

    tot_n = sum(v[0] for v in per_zoom.values())
    tot_b = sum(v[1] for v in per_zoom.values())
    print("Zoom  Kacheln   Daten")
    for z in sorted(per_zoom):
        print(f"{z:>4}  {per_zoom[z][0]:>7}  {per_zoom[z][1]/1024:>8.1f} KB")
    print(f"Summe {tot_n:>7}  {tot_b/1024:>8.1f} KB   davon verschieden: {n_unique}   "
          f"Datei: {os.path.getsize(out)/1024:.1f} KB   ({time.time()-t0:.1f} s)")
    return dict(tiles=tot_n, bytes=tot_b, minzoom=minzoom, maxzoom=maxzoom, out=out)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?")
    p.add_argument("-o", "--output")
    p.add_argument("--crs", help="CRS der Eingabe, z. B. EPSG:31467 (ASC hat keins)")
    p.add_argument("--name"); p.add_argument("--unit", default=""); p.add_argument("--attribution")
    p.add_argument("--cmap", default="viridis")
    p.add_argument("--vmin", type=float); p.add_argument("--vmax", type=float)
    p.add_argument("--gamma", type=float, default=1.0, help="<1 spreizt niedrige Werte")
    p.add_argument("--scale", type=float, default=1.0, help="Rohwerte * scale (z. B. 0.1 bei 1/10 Grad)")
    p.add_argument("--opacity", type=float, default=1.0)
    p.add_argument("--minzoom", type=int); p.add_argument("--extra-zoom", type=int, default=1)
    p.add_argument("--maxzoom", type=int, help="Obergrenze; nie hoeher als nativ + --extra-zoom")
    p.add_argument("--description", help="Text fuer die MBTiles-Metadaten")
    p.add_argument("--label-maxzoom", action="store_true",
                   help="auf der hoechsten Zoomstufe den Mittelwert jeder Kachel als Zahl in die Mitte schreiben")
    p.add_argument("--label-px", type=int, default=16, help="Schriftgroesse in Kachelpixeln")
    p.add_argument("--label-decimals", type=int, default=1)
    p.add_argument("--label-suffix", default="")
    p.add_argument("--label-sep", default=",", help="Dezimaltrennzeichen")
    p.add_argument("--label-min-cover", type=float, default=0.05,
                   help="nur beschriften, wenn mindestens dieser Anteil der Kachel Daten hat")
    p.add_argument("--tile-size", type=int, default=256, choices=[256, 512])
    p.add_argument("--resample", choices=["nearest", "bilinear"], default="nearest")
    p.add_argument("--webp-quality", type=int, help="verlustbehaftet; ohne Angabe verlustfrei")
    p.add_argument("--mask", help="GeoJSON (EPSG:4326) zum Ausschneiden")
    p.add_argument("--mask-filter", action="append", help="KEY=VALUE, mehrfach moeglich")
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    p.add_argument("--jobs", help="JSON-Liste mit Auftraegen (Schluessel wie die Optionen)")
    return p


def main():
    p = build_parser()
    a = p.parse_args()
    if a.jobs:
        base = os.path.dirname(os.path.abspath(a.jobs))
        with open(a.jobs) as f:
            jobs = json.load(f)
        for j in jobs:
            argv = []
            for k, v in j.items():
                if k in ("input", "output", "mask") and v and not os.path.isabs(v):
                    v = os.path.join(base, v)
                if isinstance(v, list):
                    for item in v:
                        argv += [f"--{k.replace('_', '-')}", str(item)]
                else:
                    argv += [f"--{k.replace('_', '-')}", str(v)] if k not in ("input", "output") else []
            ja = p.parse_args(argv)
            ja.input, ja.output = j["input"], j["output"]
            for k in ("input", "output"):
                if not os.path.isabs(getattr(ja, k)):
                    setattr(ja, k, os.path.join(base, getattr(ja, k)))
            if not os.path.exists(ja.input):
                print(f"--- uebersprungen (Eingabe fehlt): {ja.input}\n")
                continue
            print(f"=== {j.get('name', ja.input)}")
            run(ja)
            print()
        return
    if not a.input or not a.output:
        p.error("input und -o/--output sind noetig (oder --jobs)")
    run(a)


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    main()
