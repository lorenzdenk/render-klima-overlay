#!/usr/bin/env python3
"""Rasterlayer aus einem WCS-Dienst einmalig als GeoTIFF laden.

  fetch_wcs.py capabilities <URL> <logdir>          Capabilities (WCS 2.0.1/1.1.1/1.0.0, WMS) speichern, Coverage-IDs auflisten
  fetch_wcs.py get <URL> <layer> <out.tif> <logdir> Layer laden (Name exakt oder als Endung einer Coverage-ID)

Laden: zuerst eine einzige GetCoverage-Anfrage fuer den ganzen Layer (WCS 2.0.1, dann 1.0.0); liefert der
Server kein gueltiges GeoTIFF (z. B. Groessenlimit), uebernimmt der GDAL-WCS-Treiber, der in Bloecken laedt.
Ergebnis: GeoTIFF im Original-Gitter und -CRS, DEFLATE-komprimiert, gekachelt.
"""
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "render-klima-overlay (+https://github.com/lorenzdenk/render-klima-overlay)"}


def get(url, dest=None, timeout=600):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if dest is None:
            return r.read(), ctype
        with open(dest, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        return None, ctype


def coverage_ids(xml_bytes):
    ids = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ids
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("CoverageId", "Identifier", "name") and el.text and el.text.strip():
            # WCS 2.0: CoverageSummary/CoverageId, 1.1: CoverageSummary/Identifier, 1.0: CoverageOfferingBrief/name
            ids.append(el.text.strip())
    return list(dict.fromkeys(ids))


def capabilities(url, logdir):
    os.makedirs(logdir, exist_ok=True)
    allids = {}
    for svc, ver in (("WCS", "2.0.1"), ("WCS", "1.1.1"), ("WCS", "1.0.0"), ("WMS", "1.3.0")):
        u = f"{url}?SERVICE={svc}&VERSION={ver}&REQUEST=GetCapabilities"
        try:
            data, _ = get(u, timeout=120)
        except Exception as e:
            print(f"{svc} {ver}: Fehler {e}")
            continue
        fn = os.path.join(logdir, f"capabilities_{svc}_{ver}.xml")
        open(fn, "wb").write(data)
        ids = coverage_ids(data) if svc == "WCS" else []
        if svc == "WMS":
            ids = [m for m in re.findall(rb"<Name>([^<]+)</Name>", data)]
            ids = [i.decode() for i in ids]
        allids[f"{svc} {ver}"] = ids
        print(f"{svc} {ver}: {len(data)} Bytes, {len(ids)} Namen -> {fn}")
    with open(os.path.join(logdir, "coverages.txt"), "w") as f:
        for k, ids in allids.items():
            f.write(f"## {k}\n" + "\n".join(ids) + "\n\n")
    return allids


def resolve(layer, ids):
    if layer in ids:
        return layer
    cands = [i for i in ids if re.search(r"(^|[:_.])" + re.escape(layer) + r"$", i)]
    return cands[0] if len(cands) == 1 else None


def is_geotiff(path):
    try:
        out = subprocess.run(["gdalinfo", path], capture_output=True, text=True, timeout=120)
        return out.returncode == 0 and "Size is" in out.stdout
    except Exception:
        return False


def fetch(url, layer, out, logdir):
    os.makedirs(logdir, exist_ok=True)
    caps = os.path.join(logdir, "coverages.txt")
    ids20, ids10 = [], []
    if os.path.exists(caps):
        sect = None
        for line in open(caps):
            line = line.strip()
            if line.startswith("## "):
                sect = line[3:]
            elif line and sect == "WCS 2.0.1":
                ids20.append(line)
            elif line and sect == "WCS 1.0.0":
                ids10.append(line)
    c20, c10 = resolve(layer, ids20), resolve(layer, ids10)
    print(f"{layer}: Coverage-ID WCS 2.0.1 = {c20}, WCS 1.0.0 = {c10}")
    if not c20 and not c10:
        print(f"{layer}: nicht im Dienst gefunden. Kandidaten:")
        key = re.sub(r"_(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec).*$", "", layer)
        for i in sorted(set(ids20 + ids10)):
            if key.split("_")[0] in i.lower():
                print("   ", i)
        return False
    raw = out + ".raw"
    tries = []
    if c20:
        tries.append(f"{url}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={c20}&FORMAT=image/tiff")
    if c10:
        # 1.0.0 ohne CRS/BBOX -> ganzer Layer, falls der Server das zulaesst
        tries.append(f"{url}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={c10}&FORMAT=GeoTIFF")
    for u in tries:
        print("GetCoverage:", u)
        try:
            _, ctype = get(u, raw)
        except Exception as e:
            print("  Fehler:", e)
            continue
        size = os.path.getsize(raw)
        print(f"  {size/1e6:.1f} MB, Content-Type {ctype}")
        if is_geotiff(raw):
            break
        print("  kein lesbares Raster:", open(raw, "rb").read(600).decode("utf-8", "replace"))
        os.remove(raw)
    if not os.path.exists(raw):
        for ver, cid in (("2.0.1", c20), ("1.0.0", c10)):
            if not cid:
                continue
            src = f"WCS:{url}?version={ver}&coverage={cid}"
            print("GDAL-WCS-Treiber:", src)
            r = subprocess.run(["gdal_translate", "-q", src, raw], capture_output=True, text=True)
            print(r.stdout[-2000:], r.stderr[-2000:])
            if r.returncode == 0 and is_geotiff(raw):
                break
            if os.path.exists(raw):
                os.remove(raw)
    if not os.path.exists(raw):
        print(f"{layer}: Laden fehlgeschlagen")
        return False
    subprocess.run(["gdal_translate", "-q", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3", "-co", "TILED=YES",
                    "-co", "BIGTIFF=IF_SAFER", "-ot", "Float32", raw, out], check=True)
    os.remove(raw)
    info = subprocess.run(["gdalinfo", "-stats", out], capture_output=True, text=True).stdout
    open(os.path.join(logdir, f"{layer}.gdalinfo.txt"), "w").write(info)
    print(info[:3000])
    for f in os.listdir(os.path.dirname(os.path.abspath(out))):
        if f.endswith(".aux.xml"):
            os.remove(os.path.join(os.path.dirname(os.path.abspath(out)), f))
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "capabilities":
        capabilities(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 6 and sys.argv[1] == "get":
        sys.exit(0 if fetch(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]) else 1)
    else:
        sys.exit(__doc__)
