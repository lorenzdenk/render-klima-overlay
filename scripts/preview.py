#!/usr/bin/env python3
"""Vorschaubilder: die Overlay-Kacheln mit dem meisten Inhalt je Zoomstufe über OpenTopoMap gelegt.
usage: preview.py <in.mbtiles> <out dir> <zoom> [<zoom> ...]"""
import io
import os
import sqlite3
import sys
import time
import urllib.request

from PIL import Image

UA = {"User-Agent": "render-klima-overlay preview (+https://github.com/lorenzdenk/render-klima-overlay)"}


def otm(z, x, y):
    """512-px-Grundkarte aus vier OpenTopoMap-Kacheln der nächsten Stufe."""
    img = Image.new("RGBA", (512, 512), (240, 240, 240, 255))
    for dx in (0, 1):
        for dy in (0, 1):
            u = f"https://a.tile.opentopomap.org/{z + 1}/{2 * x + dx}/{2 * y + dy}.png"
            try:
                with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as r:
                    img.paste(Image.open(io.BytesIO(r.read())).convert("RGBA"), (256 * dx, 256 * dy))
            except Exception as e:
                print("  Grundkarte fehlt:", u, e)
            time.sleep(0.3)
    return img


db = sqlite3.connect(sys.argv[1])
out = sys.argv[2]
os.makedirs(out, exist_ok=True)
for z in map(int, sys.argv[3:]):
    rows = db.execute("SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=? "
                      "ORDER BY length(tile_data) DESC LIMIT 2", (z,)).fetchall()
    for x, row, data in rows:
        y = (1 << z) - 1 - row
        over = Image.open(io.BytesIO(data)).convert("RGBA")
        base = otm(z, x, y)
        both = Image.new("RGBA", (1024 + 8, 512), (255, 255, 255, 255))
        both.paste(base, (0, 0))
        mix = base.copy(); mix.alpha_composite(over)
        both.paste(mix, (520, 0))
        both.convert("RGB").save(f"{out}/vorschau_z{z}_{x}_{y}.jpg", quality=88)
        print(f"Vorschau z{z}/{x}/{y} ({len(data) / 1e3:.0f} kB)")
