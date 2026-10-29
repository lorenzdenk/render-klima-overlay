# Klima-Overlay

Klimaraster als **MBTiles-Overlay** für LoriMaps: 512-px-Kacheln, WebP mit Transparenz, identische Kacheln nur
einmal gespeichert (gleiches Schema wie `lorimaps-render` und `render-baumhoehen-overlay`).
Gerendert wird nur so weit, wie die Daten reichen: Die höchste Zoomstufe ergibt sich aus der Zellgröße
(30 m → z11, 1 km → z6 bei 512-px-Kacheln) und wird durch `MAXZOOM` gedeckelt.

> **GitHub:** Ein Push auf `render.env`, `overlays/` oder die Render-Skripte startet „Render Klima-Overlay“
> (oder manuell mit Eingabe der Overlays). Ergebnis im Release `klima-tasmanien`, Logs und Vorschaubilder im
> Branch `render-logs-<overlay>`. Die Quelldaten lädt vorher **einmal** der Workflow „Prepare Tasmania LIST climate“.

## Tasmanien: Zelten 20.11.–07.12.

| Overlay | Inhalt | Quell-Layer |
|---|---|---|
| `tas-tmin-fenster` | mittlere Tiefsttemperatur, °C | `min_avg_nov`, `min_avg_dec` |
| `tas-niederschlag-fenster` | mittlere Monatssumme Niederschlag, mm/Monat | `ra_avg_nov`, `ra_avg_dec` |
| `tas-frostnaechte` | Nächte pro Jahr mit Tmin < 1 °C, 25.11.–31.12. (unverändert) | `fr_lt1_2511to3112` |
| `tas-anu-tmin-fenster` | wie `tas-tmin-fenster`, aber aus **ANUClimate 2.0** (0,01° ≈ 1 km, Monatsmittel 1991–2020, bis Zoom 8) zum Vergleich | NCI THREDDS `gh70/ANUClimate/v2-0/stable/month/tmin` |

Tmin und Niederschlag sind das **gewichtete Mittel** von November und Dezember mit 11 : 7, also nach der Zahl der Tage, die das Fenster
20.11.–07.12. in jedem Monat hat. Der Niederschlag bleibt dabei eine Monatssumme (mm/Monat); für die Menge im
Fenster selbst ginge `OP=sum` mit `WEIGHTS="0.36667 0.22581"` (11/30 bzw. 7/31).

Quelle: **Digital Climate Maps of Tasmania** (theLIST, Tasmanische Regierung), Monatsmittel, 30 m, „recent climate“ bis 2018,
über den Dienst `https://spatial.dpipwe.tas.gov.au/naturalassets/Climate/ows` (WCS, Layer `Climate:<name>`).
Raster: UTM 55S (EPSG:32755), 17 321 × 18 763 Zellen, Meer = NaN. Lizenz auf data.gov.au: nicht angegeben –
vor einer Veröffentlichung bei nasis@nre.tas.gov.au nachfragen.
Zahlen (`LABELS=1`): Mittelwert je Feld, `LABEL_GRID` Felder je Kachelseite ab `LABEL_MINZOOM` (Tmin: 1 je Kachel auf z11, Niederschlag: 4 × 4 ab z9, Frostnächte: 10 × 10 ab z9, ANU-Tmin: 10 × 10 ab z6).
Die Tmin-Werte sind Monatsmittel der täglichen Tiefsttemperatur, offenbar mit Höhenkorrektur interpoliert: Kaltluftseen in
Tälern und Senken sind darin kaum enthalten, dort ist es in klaren Nächten kälter als angezeigt.
Der Dienst hat auch Projektionen (`_2030`, `_2050`, `_2070`, `_2100`), Tmax (`max_avg_<monat>`) und weitere
Kennwerte (z. B. `max_mt30_1611to3112` = Tage über 30 °C); Liste im Branch `prepare-logs` (`coverages.txt`).

## Workflows

| Workflow | Zweck |
|---|---|
| Prepare ANUClimate | ANUClimate-Monatswerte (NCI THREDDS, OPeNDAP) nur für den Ausschnitt laden, je Monat über die Jahre mitteln → Release `anu-daten` |
| Prepare Tasmania LIST climate | Layer per WCS einmal laden → Release `tas-list-daten` (`<layer>.tif`). Vorhandene werden übersprungen. Ohne Layerangabe nur Capabilities lesen. |
| Render Klima-Overlay | `overlays/<name>.env`: Quellraster aus `DATA_RELEASE` → `combine_months.py` → `render_mbtiles.py` → Release `RELEASE` (MBTiles, Legende, Metadaten) |

Neues Overlay: Datei in `overlays/` anlegen (Vorlage: `tas-tmin-fenster.env`), fehlende Quell-Layer mit „Prepare“ laden,
Overlay in `render.env` eintragen oder den Render-Workflow manuell mit seinem Namen starten.

## Skripte

| Skript | Zweck |
|---|---|
| `scripts/render_mbtiles.py` | Raster (ESRI-ASCII, auch `.gz`, oder GeoTIFF, beliebiges CRS) → eingefärbtes WebP-MBTiles. Zoom aus der Datenauflösung, nur Kacheln mit Daten, Werte werden vor dem Einfärben resampelt. Schreibt auch Legende (`.legend.png`) und `.meta.json`. Optional Ausschneiden mit GeoJSON (`--mask`). |
| `scripts/combine_months.py` | mehrere Raster gleichen Gitters → gewichtetes Mittel oder gewichtete Summe (blockweise, wenig RAM) |
| `scripts/thredds_fetch.py` | THREDDS-Katalog auflisten; Monatswerte per OPeNDAP ausschneiden und über Jahre mitteln |
| `scripts/fetch_wcs.py` | WCS-Capabilities lesen, Layer als GeoTIFF laden (ganzer Layer in einer Anfrage, sonst GDAL-WCS blockweise) |
| `scripts/preview.py` | Vorschaubilder: Overlay-Kacheln über OpenTopoMap |
| `silo/` | lokale Tageszählungen aus SILO (5 km, 1991–2020): Tage mit Tmin ≤ 10 °C, Regentage ≥ 1 mm, Tage mit Bewölkung ≤ 3 Achtel im Fenster. `python silo/run_tasmania.py --smoke`; lädt ca. 0,4 GB je Jahr und Variable. |
| `dwd/jobs.json` | Deutschland (DWD CDC, 1 km): Schneedecken-Tage und Niederschlag 1991–2020, `python scripts/render_mbtiles.py --jobs dwd/jobs.json` (Eingaben von opendata.dwd.de daneben legen) |

Lokal: `pip install -r requirements.txt`.
