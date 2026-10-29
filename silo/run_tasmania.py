#!/usr/bin/env python3
"""
run_tasmania.py - Startet alle Ebenen aus tasmania_jobs.json:
SILO-Daten laden -> Tage im Fenster zaehlen (.asc) -> WebP-MBTiles rendern.

  python run_tasmania.py --dry-run          # nur anzeigen, was passieren wuerde (laedt nichts)
  python run_tasmania.py --smoke            # Schnelltest mit 3 Jahren (2018-2020, ca. 3-4 GB Download)
  python run_tasmania.py                    # alles, Jahre laut tasmania_jobs.json (1991-2020)
  python run_tasmania.py --only rain_ge1mm  # nur eine Ebene

Fehler in einer Ebene stoppen die anderen nicht; am Ende gibt es eine Uebersicht.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GB_PER_FILE = 0.41


def check_deps():
    missing = []
    for mod, pipname in (("numpy", "numpy"), ("xarray", "xarray"), ("pyproj", "pyproj"),
                         ("PIL", "pillow"), ("matplotlib", "matplotlib")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pipname)
    try:
        __import__("netCDF4")
    except ImportError:
        try:
            __import__("h5netcdf")
        except ImportError:
            missing.append("netCDF4")
    if missing:
        sys.exit("Fehlende Pakete: " + ", ".join(missing) + "\n  pip install " + " ".join(missing))
    from PIL import features
    if not features.check("webp"):
        sys.exit("Pillow hat keine WebP-Unterstuetzung (pip install --upgrade pillow).")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(HERE, "tasmania_jobs.json"))
    p.add_argument("--years", help="ueberschreibt 'years' aus der Konfiguration, z. B. 2011-2020")
    p.add_argument("--smoke", action="store_true", help="Schnelltest: Jahre 2018-2020")
    p.add_argument("--only", help="Komma-getrennte Job-IDs")
    p.add_argument("--dir", help="Ordner fuer lokale/heruntergeladene SILO-Dateien (Standard: <out>/_silo_tmp)")
    p.add_argument("--out", default=os.path.join(os.getcwd(), "tasmania_out"))
    p.add_argument("--keep", action="store_true", help="heruntergeladene SILO-Dateien behalten (spart Wiederholung)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    years = "2018-2020" if a.smoke else (a.years or cfg["years"])
    only = set(a.only.split(",")) if a.only else None
    jobs = [j for j in cfg["jobs"] if j.get("enabled", True) and (only is None or j["id"] in only)]
    if not jobs:
        sys.exit("Keine passenden Jobs.")
    if not a.dry_run:
        check_deps()
    os.makedirs(a.out, exist_ok=True)
    workdir = a.dir or os.path.join(a.out, "_silo_tmp")
    w = cfg["window"]

    if "-" in years:
        s, e = years.split("-"); nyears = int(e) - int(s) + 1
    else:
        nyears = len(years.split(","))
    n_files = 0
    for j in jobs:
        folder, stem = j["folder"], j["stem"]
        have = 0
        if "-" in years:
            for y in range(int(s), int(e) + 1):
                have += os.path.exists(os.path.join(workdir, folder, f"{y}.{stem}.nc"))
        n_files += nyears - have
    print(f"Jobs: {', '.join(j['id'] for j in jobs)}")
    print(f"Jahre: {years}  Fenster: {w['start']} bis {w['end']}  Ausgabe: {a.out}")
    print(f"Zu ladende Dateien: {n_files}  (~{n_files*GB_PER_FILE:.1f} GB Download, je Datei ~0,4 GB; "
          f"danach wird geloescht, Platzbedarf ca. 0,5 GB)\n")

    results = []
    for j in jobs:
        asc = os.path.join(a.out, f"tasmania_{j['id']}.asc")
        mbt = os.path.join(a.out, f"tasmania_{j['id']}.mbtiles")
        count_cmd = [sys.executable, "-I", os.path.join(HERE, "silo_window_days.py"),
                     "--years", years, "--start", w["start"], "--end", w["end"],
                     "--var", j["var"], "--folder", j["folder"], "--stem", j["stem"],
                     "--op", j["op"], "--threshold", str(j["threshold"]),
                     "--dir", workdir, "--out", asc]
        if j.get("expect_range"):
            count_cmd += ["--expect-range", str(j["expect_range"][0]), str(j["expect_range"][1])]
        if a.keep:
            count_cmd.append("--keep")
        render_cmd = [sys.executable, "-I", os.path.join(HERE, "..", "scripts", "render_mbtiles.py"), asc, "-o", mbt,
                      "--crs", "EPSG:4326", "--name", j["name"], "--unit", j.get("unit", ""),
                      "--cmap", j["cmap"], "--vmin", str(j["vmin"]), "--vmax", str(j["vmax"]),
                      "--extra-zoom", str(j.get("extra_zoom", 2)),
                      "--attribution", cfg.get("attribution", "")]
        print(f"=== {j['id']}: {j['name']}")
        if a.dry_run:
            print("  1) " + " ".join(count_cmd)); print("  2) " + " ".join(render_cmd)); print()
            continue
        t0 = time.time()
        ok = subprocess.run(count_cmd).returncode == 0 and subprocess.run(render_cmd).returncode == 0
        results.append((j["id"], ok, mbt if ok else "-", time.time() - t0))
        print()

    if a.dry_run:
        return
    print("=== Uebersicht")
    for jid, ok, path, dt in results:
        print(f"  {'OK    ' if ok else 'FEHLER'} {jid:22s} {dt/60:5.1f} min  {path}")
    if not all(r[1] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
