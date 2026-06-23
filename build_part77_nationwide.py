"""
build_part77_nationwide.py
==========================
Generate nationwide 14 CFR Part 77.19 civil-airport imaginary-surface
2D footprints from FAA NASR data, using only open-source tools.

INPUT (download yourself — these hosts are not reachable from the build sandbox):
  FAA 28-Day NASR Subscription, CSV format:
    https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/
  You need two files from the APT_*.csv set:
    APT_RWY.csv      (runway-level: width, surface type)
    APT_RWY_END.csv  (per-end: lat/lon, approach type, visibility minima)

OUTPUT:
  part77_footprints.gpkg  — one polygon per runway, EPSG:4326, plus a
                            dissolved 'part77_dissolved' layer (all surfaces
                            merged into a single exclusion mask).

USAGE:
  python build_part77_nationwide.py --rwy APT_RWY.csv --end APT_RWY_END.csv \
         --out part77_footprints.gpkg

NOTE ON COLUMN NAMES:
  NASR CSV headers have shifted across cycles (a format change lands with the
  03 Sept 2026 cycle). This script resolves columns by fuzzy matching on a set
  of candidate names; if a required field can't be found it prints the actual
  headers so you can map them with the --map option. Run with --inspect first
  to dump headers without processing.
"""

from __future__ import annotations
import argparse, sys, csv, math, re
from collections import defaultdict

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping

from part77 import (RunwayEnd, build_footprint, build_footprint_for_height,
                    PRIMARY_WIDTH_FT)


# ---------------------------------------------------------------------------
# Column resolution — tolerate NASR header drift
# ---------------------------------------------------------------------------
CANDIDATES = {
    "site_no":   ["site_no", "arpt_site_no", "site_number"],
    "rwy_id":    ["rwy_id", "runway_id", "rwy_name"],
    "rwy_end_id":["rwy_end_id", "runway_end_id", "base_end_id", "end_id", "rwy_end"],
    "lat":       ["lat_decimal", "rwy_end_lat_decimal", "true_lat", "latitude", "lat_deg"],
    "lon":       ["long_decimal", "rwy_end_long_decimal", "true_long", "longitude", "long_deg"],
    "rwy_width": ["rwy_width", "width", "runway_width"],
    "surf_type": ["surface_type_code", "rwy_surface_type", "surface_type", "cond"],
    # approach descriptors used for classification
    "rwy_end_id_join": ["rwy_id", "runway_id"],
    "vis_min":   ["rvr_eqmt", "approach_vis", "vis_minimums", "rwy_end_vis", "rvv"],
    "appr_type": ["right_hand_traffic_pat_flag", "apch_lgt_system_code",
                  "instrument_landing_system", "appr_type", "apch_type"],
    # The most reliable proxies present in APT_RWY_END.csv:
    "rh_ils":    ["ils_type", "instrument_landing_system_type"],
    "rwy_end_designator": ["rwy_end_id", "end_id"],
    # Authoritative FAA Part 77 approach category, when populated.
    "far_p77":   ["far_part_77_code", "far_part77_code", "part_77_code"],
}


# FAA FAR Part 77 approach-category codes -> engine classification.
# Present in ~60% of NASR runway ends; the rest fall back to derive_flags().
FAR_P77_MAP = {
    "A(V)":  "UTIL_VIS",   # utility, visual
    "A(NP)": "UTIL_NPI",   # utility, non-precision instrument
    "B(V)":  "VIS",        # larger-than-utility, visual
    "B(NP)": "NPI_GT",     # larger-than-utility, non-precision (rare; treat as C)
    "C":     "NPI_GT",     # non-precision, visibility minimums > 3/4 mile
    "D":     "NPI_LOW",    # non-precision, visibility minimums as low as 3/4 mile
    "PIR":   "PIR",        # precision instrument runway
}


def classify_from_far77(code):
    """Map a FAR_PART_77_CODE value to an engine class, or None if blank/unknown."""
    if not code:
        return None
    return FAR_P77_MAP.get(str(code).strip().upper())


def resolve(cols, key):
    norm = {re.sub(r'[^a-z0-9]', '', c.lower()): c for c in cols}
    for cand in CANDIDATES[key]:
        k = re.sub(r'[^a-z0-9]', '', cand.lower())
        if k in norm:
            return norm[k]
    # loose contains-match
    for cand in CANDIDATES[key]:
        k = re.sub(r'[^a-z0-9]', '', cand.lower())
        for nk, orig in norm.items():
            if k in nk or nk in k:
                return orig
    return None


# ---------------------------------------------------------------------------
# Runway-end classification (77.19 categories)
# ---------------------------------------------------------------------------
# We classify each END independently, then build_footprint takes the more
# demanding of the two for primary width / horizontal radius, while applying
# each end's own approach surface.
#
# Inputs available in NASR per end vary; we use a robust decision tree on the
# fields most consistently populated:
#   - whether the runway is paved (surface type)
#   - whether the end has an instrument approach (ILS / approach type)
#   - visibility minimums (to split NPI_GT vs NPI_LOW and PIR)
#   - runway length/width as a utility-runway proxy
#
# Utility runway (per AC 150/5300-13 / Part 77 usage): runways used by
# small aircraft, generally < 60,000 lb; in practice approximated by
# runway length < 5,000 ft AND width <= 60 ft when no better field exists.

def classify_end(paved: bool, has_instrument: bool, precision: bool,
                 vis_low: bool, utility: bool) -> str:
    if precision:
        return "PIR"
    if has_instrument:           # non-precision instrument
        if utility:
            return "UTIL_NPI"
        return "NPI_LOW" if vis_low else "NPI_GT"
    # visual only
    return "UTIL_VIS" if utility else "VIS"


def derive_flags(row, length_ft):
    """Heuristic extraction of classification flags from a runway-end row.
    Adjust here if your NASR cycle exposes cleaner fields."""
    # Instrument approach present?
    appr_blob = " ".join(str(row.get(c, "")) for c in row.index).upper()
    # Precision: ILS/PAR/GLS/CAT I-III or the word PRECISION *not* preceded by NON.
    precision = bool(
        re.search(r"\bILS\b|\bPAR\b|\bGLS\b|CAT\s?I{1,3}\b", appr_blob)
        or re.search(r"(?<!NON.)(?<!NON)\bPRECISION\b", appr_blob)
    )
    has_instrument = precision or bool(
        re.search(r"RNAV|GPS|LNAV|VOR|NDB|\bLOC\b|RNP|NON-?PRECISION|LDA|SDF", appr_blob)
    )
    # Visibility as low as 3/4 mile (≈ RVR 4000) -> vis_low True
    vis_low = bool(re.search(r"\b(40|3/4|0\.75|4000)\b", appr_blob))
    # Utility proxy
    utility = (length_ft is not None and length_ft < 5000)
    return has_instrument, precision, vis_low, utility


def load(rwy_csv, end_csv, inspect=False):
    rwy = pd.read_csv(rwy_csv, dtype=str, low_memory=False)
    end = pd.read_csv(end_csv, dtype=str, low_memory=False)
    if inspect:
        print("APT_RWY columns:\n ", list(rwy.columns))
        print("\nAPT_RWY_END columns:\n ", list(end.columns))
        sys.exit(0)
    return rwy, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rwy", required=True, help="APT_RWY.csv")
    ap.add_argument("--end", required=True, help="APT_RWY_END.csv")
    ap.add_argument("--out", default="part77_footprints.gpkg")
    ap.add_argument("--inspect", action="store_true",
                    help="print CSV headers and exit")
    ap.add_argument("--limit", type=int, default=None,
                    help="process only N runways (for testing)")
    ap.add_argument("--tower-height-m", type=float, default=None,
                    help="if set, output the height-aware exclusion zones where "
                         "a structure of this height (meters) would penetrate a "
                         "77.19 surface, instead of the full 2D surface footprint")
    args = ap.parse_args()

    rwy, end = load(args.rwy, args.end, inspect=args.inspect)

    c_site_r = resolve(rwy.columns, "site_no")
    c_rid_r  = resolve(rwy.columns, "rwy_id")
    c_width  = resolve(rwy.columns, "rwy_width")
    c_surf   = resolve(rwy.columns, "surf_type")

    c_site_e = resolve(end.columns, "site_no")
    c_rid_e  = resolve(end.columns, "rwy_id")
    c_lat    = resolve(end.columns, "lat")
    c_lon    = resolve(end.columns, "lon")
    c_eid    = resolve(end.columns, "rwy_end_designator")
    c_p77    = resolve(end.columns, "far_p77")

    missing = {k: v for k, v in {
        "site_no(rwy)": c_site_r, "rwy_id(rwy)": c_rid_r,
        "site_no(end)": c_site_e, "rwy_id(end)": c_rid_e,
        "lat": c_lat, "lon": c_lon,
    }.items() if v is None}
    if missing:
        print("ERROR: could not resolve required columns:", missing)
        print("\nRun with --inspect to view headers, then edit CANDIDATES.")
        sys.exit(1)

    # runway-level lookups
    rwy_len_col = resolve(rwy.columns, "rwy_id")  # placeholder; length below
    # length column is often 'rwy_len' / 'length'
    length_col = None
    for cand in ("rwy_len", "length", "runway_length"):
        k = re.sub(r'[^a-z0-9]', '', cand)
        for col in rwy.columns:
            if k == re.sub(r'[^a-z0-9]', '', col.lower()):
                length_col = col; break
        if length_col: break

    paved_set = set()
    length_map = {}
    for _, r in rwy.iterrows():
        key = (r[c_site_r], r[c_rid_r])
        surf = str(r.get(c_surf, "")).upper() if c_surf else ""
        paved = any(s in surf for s in ("ASPH", "CONC", "PEM", "BIT", "TURF-A"))
        if paved:
            paved_set.add(key)
        if length_col:
            try:
                length_map[key] = float(r[length_col])
            except (ValueError, TypeError):
                pass

    # group ends by runway
    ends_by_rwy = defaultdict(list)
    for _, e in end.iterrows():
        try:
            lat = float(e[c_lat]); lon = float(e[c_lon])
        except (ValueError, TypeError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        ends_by_rwy[(e[c_site_e], e[c_rid_e])].append(e)

    def classify(row, paved, length_ft):
        """Prefer the authoritative FAR Part 77 code; fall back to heuristic."""
        code = row.get(c_p77) if c_p77 else None
        cls = classify_from_far77(code)
        if cls is not None:
            return cls, "far77"
        return classify_end(paved, *derive_flags(row, length_ft)), "heuristic"

    records = []
    count = 0
    n_skipped = 0
    src_counts = defaultdict(int)
    for key, ends in ends_by_rwy.items():
        if len(ends) < 2:
            continue  # need both thresholds to define a centerline
        e1, e2 = ends[0], ends[1]
        try:
            lat1, lon1 = float(e1[c_lat]), float(e1[c_lon])
            lat2, lon2 = float(e2[c_lat]), float(e2[c_lon])
        except (ValueError, TypeError):
            continue
        length_ft = length_map.get(key)
        paved = key in paved_set

        cls1, src1 = classify(e1, paved, length_ft)
        cls2, src2 = classify(e2, paved, length_ft)
        src_counts[src1] += 1
        src_counts[src2] += 1

        rw = RunwayEnd(lat1, lon1, lat2, lon2, cls1, cls2,
                       paved=paved, ident=f"{key[0]}/{key[1]}")
        try:
            if args.tower_height_m is not None:
                fp = build_footprint_for_height(rw, args.tower_height_m)
            else:
                fp = build_footprint(rw)
        except Exception as ex:
            print(f"skip {rw.ident}: {ex}")
            n_skipped += 1
            continue
        if fp is None or fp.is_empty:
            n_skipped += 1
            continue
        records.append({
            "site_no": key[0], "rwy_id": key[1],
            "cls1": cls1, "cls2": cls2, "paved": paved,
            "geometry": fp,
        })
        count += 1
        if args.limit and count >= args.limit:
            break

    if not records:
        print("No footprints produced — check column mapping with --inspect.")
        sys.exit(1)

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    gdf.to_file(args.out, layer="part77_by_runway", driver="GPKG")

    # geopandas >= 1.0 exposes union_all(); 0.14 uses the unary_union property.
    # Silence harmless GEOS FP warnings during the nationwide dissolve.
    with np.errstate(divide="ignore", invalid="ignore"):
        dissolved_geom = (
            gdf.geometry.union_all() if hasattr(gdf.geometry, "union_all")
            else gdf.geometry.unary_union
        )
    dissolved = gpd.GeoDataFrame(
        geometry=[dissolved_geom], crs="EPSG:4326"
    )
    dissolved.to_file(args.out, layer="part77_dissolved", driver="GPKG")

    print(f"Wrote {len(gdf)} runway footprints to {args.out}")
    print("Layers: part77_by_runway, part77_dissolved")
    if n_skipped:
        print(f"Skipped {n_skipped} runways with degenerate geometry.")
    print(f"Runway ends classified by: {dict(src_counts)} "
          f"(far77 = authoritative FAA code, heuristic = derive_flags fallback)")


if __name__ == "__main__":
    main()
