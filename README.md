# Nationwide FAA Part 77.19 — 2D Footprint Generator (open-source)

Generates **2D ground footprints** of the civil-airport imaginary surfaces
defined in **14 CFR § 77.19**, nationwide, with no ArcGIS / Aviation license.
Pure Python: `shapely`, `pyproj`, `geopandas`, `pandas`.

Use the output as **exclusion zones** for screening tall objects (e.g. a 100 m
structure). A 100 m object exceeds Part 77 notice criteria almost everywhere
near an airport, so any intersection with these footprints flags a site that
needs a real obstruction evaluation (FAA Form 7460-1).

> These are derived screening surfaces, not an official FAA determination.
> Always confirm with the FAA OE/AAA process before relying on a result.

## Files
- `part77.py` — geometry engine. Builds primary, approach, horizontal,
  conical, and transitional footprints per runway and dissolves them.
- `build_part77_nationwide.py` — reads FAA NASR CSVs, classifies each runway
  end, runs the engine nationwide, writes a GeoPackage.

## Install
```
pip install geopandas shapely pyproj pandas
```

## Get the data (do this yourself — large, updated every 28 days)
FAA 28-Day NASR Subscription, **CSV** format:
https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/

From the `APT_*.csv` set you need:
- `APT_RWY.csv` — runway width, surface type, length
- `APT_RWY_END.csv` — per-end lat/lon and approach descriptors

(Alternative, pre-joined and public-domain: the BTS NTAD "Runways" layer,
derived from the same NASR files. Either works; the script targets the raw
NASR CSVs.)

## Run
```
# 1. confirm the column names in your cycle's CSVs
python build_part77_nationwide.py --rwy APT_RWY.csv --end APT_RWY_END.csv --inspect

# 2. build everything
python build_part77_nationwide.py --rwy APT_RWY.csv --end APT_RWY_END.csv \
       --out part77_footprints.gpkg

# optional: --limit 50 to test on a subset first
```

Output `part77_footprints.gpkg` has two layers (EPSG:4326):
- `part77_by_runway` — one polygon per runway, with classification attributes
- `part77_dissolved` — all surfaces merged into a single exclusion mask

Open in QGIS, or load with `geopandas.read_file(path, layer=...)`.

## How each surface is built (per § 77.19)
- **Primary** (c): centered on runway; width by classification
  (250/500/1000 ft); extends 200 ft past each paved runway end.
- **Approach** (d): trapezoid per end, inner width = primary width, flaring to
  the outer width over the regulated length — precision is 16,000 ft wide over
  50,000 ft (10,000 @ 50:1 + 40,000 @ 40:1).
- **Horizontal** (a): 5,000 ft (utility/visual) or 10,000 ft arcs swung from
  each primary end, joined by tangents → stadium/oval.
- **Conical** (b): horizontal radius + 4,000 ft.
- **Transitional** (e): 7:1 from the sides of primary/approach; its 2D extent
  (1,050 ft to reach the 150 ft horizontal plane) is added alongside the
  approach corridors, then everything is dissolved into one footprint.

Geometry is computed in a per-runway azimuthal-equidistant projection (meters)
for accuracy, then reprojected to WGS84.

## Classification (the part to tune)
Each runway END is classified independently into one of:
`UTIL_VIS, UTIL_NPI, VIS, NPI_GT, NPI_LOW, PIR`.
The engine uses the more demanding of the two ends for primary width and
horizontal radius, while applying each end's own approach surface
(per 77.19(c)(iv) and 77.19(d)(3)).

`derive_flags()` in `build_part77_nationwide.py` infers the flags
(instrument? precision? low visibility? utility?) from the approach
descriptor text and runway length. **This is the heuristic layer** — if your
NASR cycle exposes cleaner approach/visibility-minima fields, map them there
for higher fidelity. Utility is currently approximated as runway length
< 5,000 ft; replace with the authoritative design-group field if you have it.

## Caveats
- NASR CSV headers shift between cycles (a format change lands 03 Sep 2026).
  The loader fuzzy-matches column names and `--inspect` dumps the real headers
  so you can extend the `CANDIDATES` map.
- Runways missing a valid coordinate on either end are skipped.
- These are civil-airport surfaces (§ 77.19). Heliports (§ 77.21) and military
  fields use different specs and are out of scope here.
# FAA_height_restriction_layer
