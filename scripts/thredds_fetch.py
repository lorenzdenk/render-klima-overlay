#!/usr/bin/env python3
"""THREDDS (z. B. NCI) erkunden und Monatswerte einmalig als GeoTIFF-Mittel herunterladen.

  thredds_fetch.py list  <catalog.xml-URL> [--depth 3]
      Katalog rekursiv auflisten (Unterkataloge, Datensätze mit OPeNDAP-Pfad).

  thredds_fetch.py fetch --catalog <catalog.xml-URL> --years 1991-2020 --months 11 12 \
                         --bbox 143.5 -43.8 148.6 -39.5 --out data/anu_tmin [--var tmin]
      Findet die Datensätze der Jahre (Dateien pro Jahr oder pro Monat, erkannt an JJJJ bzw. JJJJMM im Namen),
      liest per OPeNDAP nur den Ausschnitt und die gewünschten Monate, mittelt je Monat über alle Jahre und
      schreibt <out>_<MM>.tif (EPSG:4326, Float32, NaN = keine Daten). Zusätzlich <out>_<MM>.json mit den
      verwendeten Jahren.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NS = {"t": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0",
      "x": "http://www.w3.org/1999/xlink"}
UA = {"User-Agent": "render-klima-overlay (+https://github.com/lorenzdenk/render-klima-overlay)"}


def read_catalog(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        root = ET.fromstring(r.read())
    refs, dsets = [], []
    for ref in root.iter(f"{{{NS['t']}}}catalogRef"):
        href = ref.get(f"{{{NS['x']}}}href")
        if href:
            refs.append((ref.get(f"{{{NS['x']}}}title") or href, urllib.parse.urljoin(url, href)))
    for ds in root.iter(f"{{{NS['t']}}}dataset"):
        p = ds.get("urlPath")
        if p:
            dsets.append((ds.get("name") or p, p))
    return refs, dsets


def dods_url(catalog_url, url_path):
    u = urllib.parse.urlparse(catalog_url)
    return f"{u.scheme}://{u.netloc}/thredds/dodsC/{url_path}"


def cmd_list(url, depth, indent=0, seen=None):
    seen = seen if seen is not None else set()
    if url in seen or depth < 0:
        return
    seen.add(url)
    try:
        refs, dsets = read_catalog(url)
    except Exception as e:
        print("  " * indent + f"! {url}: {e}")
        return
    print("  " * indent + f"[{url}]  {len(refs)} Unterkataloge, {len(dsets)} Datensätze")
    for name, p in dsets[:8]:
        print("  " * indent + f"  - {name}  ->  {dods_url(url, p)}")
    if len(dsets) > 8:
        print("  " * indent + f"  ... ({len(dsets) - 8} weitere, letzter: {dsets[-1][0]})")
    for title, sub in refs[:40]:
        print("  " * indent + f"  > {title}")
        cmd_list(sub, depth - 1, indent + 2, seen)


def match_datasets(dsets, years, months):
    """{(jahr, monat|None): urlPath} aus Datensatznamen mit JJJJMM oder JJJJ."""
    found = {}
    for name, p in dsets:
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])?(?!\d)", name)
        if not m:
            continue
        y = int(m.group(1))
        mo = int(m.group(2)) if m.group(2) else None
        if y in years and (mo is None or mo in months):
            found[(y, mo)] = p
    return found


def cmd_fetch(a):
    import numpy as np
    import rasterio
    import xarray as xr
    from rasterio.transform import from_origin

    y0, y1 = (int(v) for v in a.years.split("-"))
    years = set(range(y0, y1 + 1))
    months = [int(m) for m in a.months]
    refs, dsets = read_catalog(a.catalog)
    dsets = [(n, p, a.catalog) for n, p in dsets]
    # Unterkataloge pro Jahr (z. B. .../month/tmin/1991/catalog.xml) mitlesen
    for title, sub in refs:
        m = re.fullmatch(r"\s*((?:19|20)\d{2})\s*", title or "")
        if m and int(m.group(1)) in years:
            _, sd = read_catalog(sub)
            dsets += [(n, p, sub) for n, p in sd]
    found = match_datasets([(n, p) for n, p, _ in dsets], years, months)
    print(f"{len(dsets)} Datensätze im Katalog, passend: {len(found)}")
    if not found:
        for name, _ in dsets[:20]:
            print("   ", name)
        sys.exit("Keine Datensätze für die Jahre gefunden.")
    lon0, lat0, lon1, lat1 = a.bbox
    acc, cnt, used, grid = {}, {}, {m: [] for m in months}, None
    for (y, mo), p in sorted(found.items()):
        url = dods_url(a.catalog, p)
        ds = xr.open_dataset(url, decode_times=True)
        var = a.var if a.var in ds.data_vars else next(n for n, v in ds.data_vars.items() if v.ndim == 3)
        da = ds[var]
        lat = next(d for d in da.dims if "lat" in d.lower())
        lon = next(d for d in da.dims if "lon" in d.lower())
        tim = next(d for d in da.dims if "time" in d.lower())
        lat_sl = slice(lat1, lat0) if float(ds[lat][0]) > float(ds[lat][-1]) else slice(lat0, lat1)
        da = da.sel({lon: slice(lon0, lon1), lat: lat_sl})
        for m in months:
            if mo is not None and mo != m:
                continue
            t = da[tim]
            sel = (t.dt.month == m) & (t.dt.year == y)
            if not bool(sel.any()):
                continue
            arr = da.isel({tim: np.where(sel.values)[0]}).mean(tim).load()
            arr = arr.sortby(lat, ascending=False)
            vals = arr.values.astype("float64")
            if grid is None:
                grid = (arr[lon].values, arr[lat].values)
                print(f"Variable {var!r} units={da.attrs.get('units')!r} long_name={da.attrs.get('long_name')!r}, "
                      f"Ausschnitt {vals.shape[1]} x {vals.shape[0]}")
            elif vals.shape != acc.get(m, vals).shape:
                sys.exit(f"{y}-{m:02d}: Gitter weicht ab")
            ok = np.isfinite(vals)
            acc[m] = acc.get(m, np.zeros_like(vals)) + np.where(ok, vals, 0)
            cnt[m] = cnt.get(m, np.zeros(vals.shape, int)) + ok
            used[m].append(y)
            print(f"  {y}-{m:02d}: Mittel {np.nanmean(vals):.2f}, min {np.nanmin(vals):.2f}, max {np.nanmax(vals):.2f}",
                  flush=True)
        ds.close()
    lons, lats = grid
    res_x = float(abs(lons[1] - lons[0]))
    res_y = float(abs(lats[1] - lats[0]))
    if abs(res_x - res_y) > 1e-9:
        sys.exit(f"Zellen nicht quadratisch ({res_x} x {res_y})")
    tr = from_origin(float(lons.min()) - res_x / 2, float(lats.max()) + res_y / 2, res_x, res_y)
    for m in months:
        if m not in acc:
            sys.exit(f"Monat {m}: keine Daten")
        need = max(1, int(0.8 * len(used[m])))
        mean = np.where(cnt[m] >= need, acc[m] / np.maximum(cnt[m], 1), np.nan).astype("float32")
        out = f"{a.out}_{m:02d}.tif"
        with rasterio.open(out, "w", driver="GTiff", height=mean.shape[0], width=mean.shape[1], count=1,
                           dtype="float32", crs="EPSG:4326", transform=tr, nodata=float("nan"),
                           compress="deflate", predictor=3) as dst:
            dst.write(mean, 1)
        json.dump({"months": m, "years": used[m], "source": a.catalog}, open(f"{a.out}_{m:02d}.json", "w"))
        print(f"{out}: {len(used[m])} Jahre ({min(used[m])}–{max(used[m])}), Mittel {np.nanmean(mean):.2f}, "
              f"min {np.nanmin(mean):.2f}, max {np.nanmax(mean):.2f}, gültig {int(np.isfinite(mean).sum())}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list"); l.add_argument("catalog"); l.add_argument("--depth", type=int, default=3)
    f = sub.add_parser("fetch")
    f.add_argument("--catalog", required=True); f.add_argument("--years", default="1991-2020")
    f.add_argument("--months", nargs="+", default=["11", "12"]); f.add_argument("--var", default="tmin")
    f.add_argument("--bbox", type=float, nargs=4, default=[143.5, -43.8, 148.6, -39.5])
    f.add_argument("--out", required=True)
    a = p.parse_args()
    if a.cmd == "list":
        cmd_list(a.catalog, a.depth)
    else:
        cmd_fetch(a)
