"""
part77.py — Generate 14 CFR Part 77.19 civil airport imaginary surface
2D footprints (ground polygons) from runway-end data, using only
open-source tools (Shapely + pyproj).

No ArcGIS Aviation license required.

Surfaces implemented (per 77.19):
  - Primary surface
  - Approach surface (per runway end, sized by approach category)
  - Horizontal surface (150 ft plane; built from arcs swung off primary ends)
  - Conical surface (extends 4,000 ft beyond horizontal)
  - Transitional surface (7:1; 2D footprint approximated as the ground extent
    out to where transitional meets horizontal/conical — see notes below)

IMPORTANT — what a 2D footprint means here:
  The regulation defines 3D surfaces. For a 2D screening footprint we project
  each surface to the ground. For the transitional surface the meaningful 2D
  contribution is the strip alongside the primary/approach surfaces out to the
  horizontal surface; because the horizontal+conical already cover the area
  around the runway out to a large radius, the transitional footprint is mostly
  subsumed. We still add an explicit transitional strip so the footprint is
  correct in the approach corridors that extend beyond the conical surface
  (the long precision approach surfaces).

All dimensions in 77.19 are in FEET. We build geometry in a local azimuthal
equidistant projection centered on the runway (units = meters), so we convert
feet -> meters once.
"""

from __future__ import annotations
from dataclasses import dataclass
import math

import numpy as np
from shapely.geometry import Polygon, MultiPolygon, Point
from shapely.ops import unary_union, transform as shp_transform
from pyproj import Transformer, CRS

FT = 0.3048  # feet -> meters

# ---------------------------------------------------------------------------
# Runway-end classification -> surface parameters (77.19)
# ---------------------------------------------------------------------------
# Categories:
#   UTIL_VIS   : utility runway, visual approach only
#   UTIL_NPI   : utility runway, non-precision instrument approach
#   VIS        : other-than-utility, visual approach only
#   NPI_GT     : non-precision instrument, visibility minimums > 3/4 mile
#   NPI_LOW    : non-precision instrument, minimums as low as 3/4 mile
#   PIR        : precision instrument runway

PRIMARY_WIDTH_FT = {           # 77.19(c)
    "UTIL_VIS": 250,
    "UTIL_NPI": 500,
    "VIS":      500,
    "NPI_GT":   500,
    "NPI_LOW": 1000,
    "PIR":     1000,
}

# 77.19(d): approach inner width = primary width; expands to outer width over
# a horizontal length. Precision = two segments (10,000 @50:1 + 40,000 @40:1).
APPROACH = {   # outer_width_ft, total_horizontal_length_ft
    "UTIL_VIS": (1250,  5000),
    "VIS":      (1500,  5000),
    "UTIL_NPI": (2000,  5000),   # utility NPI uses visual-runway 5,000 ft / 20:1 extent
    "NPI_GT":   (3500, 10000),
    "NPI_LOW":  (4000, 10000),
    "PIR":      (16000, 50000),  # 10,000 + 40,000
}

# 77.19(a): horizontal-surface arc radius
HORIZ_RADIUS_FT = {
    "UTIL_VIS":  5000,
    "UTIL_NPI":  5000,
    "VIS":      10000,
    "NPI_GT":   10000,
    "NPI_LOW":  10000,
    "PIR":      10000,
}

CONICAL_EXTENT_FT = 4000     # 77.19(b)
HARD_SURFACE_EXT_FT = 200    # 77.19(c) primary extends 200 ft past paved runway ends
ARC_STEP_DEG = 3             # arc densification


@dataclass
class RunwayEnd:
    """One physical runway with its two ends and their classifications."""
    lat1: float; lon1: float   # threshold of end 1
    lat2: float; lon2: float   # threshold of end 2
    cls1: str                  # classification at end 1
    cls2: str                  # classification at end 2
    paved: bool = True
    ident: str = ""


def _local_crs(lat0: float, lon0: float) -> CRS:
    """Azimuthal equidistant projection centered on the runway (meters)."""
    return CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 "
        f"+datum=WGS84 +units=m +no_defs"
    )


def _rect_from_centerline(x1, y1, x2, y2, w_inner, w_outer=None):
    """Build a (possibly trapezoidal) polygon centered on segment (1->2).
    w_inner is full width at end 1, w_outer full width at end 2."""
    if w_outer is None:
        w_outer = w_inner
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L == 0:
        return None
    # unit normal
    nx, ny = -dy / L, dx / L
    hi, ho = w_inner / 2.0, w_outer / 2.0
    return Polygon([
        (x1 + nx * hi, y1 + ny * hi),
        (x2 + nx * ho, y2 + ny * ho),
        (x2 - nx * ho, y2 - ny * ho),
        (x1 - nx * hi, y1 - ny * hi),
    ])


def _arc(cx, cy, r, a_start, a_end, step_deg=ARC_STEP_DEG):
    """Points along an arc, inclusive of both ends."""
    pts = []
    a = a_start
    sweep = a_end - a_start
    n = max(1, int(abs(math.degrees(sweep)) / step_deg))
    for i in range(n + 1):
        ang = a_start + sweep * i / n
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def build_footprint(rw: RunwayEnd) -> Polygon | MultiPolygon:
    """Return the 2D Part 77 footprint for one runway, in WGS84 lon/lat."""
    # Center the projection between the two ends.
    lat0 = (rw.lat1 + rw.lat2) / 2.0
    lon0 = (rw.lon1 + rw.lon2) / 2.0
    crs = _local_crs(lat0, lon0)
    to_local = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    x1, y1 = to_local(rw.lon1, rw.lat1)
    x2, y2 = to_local(rw.lon2, rw.lat2)

    # Most precise classification governs primary width (77.19(c)(iv)).
    prim_w = max(PRIMARY_WIDTH_FT[rw.cls1], PRIMARY_WIDTH_FT[rw.cls2]) * FT

    # Primary surface ends: extend 200 ft past paved runway ends.
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    # Guard against degenerate runways (coincident/near-coincident ends),
    # which would otherwise yield divide-by-zero and invalid geometry.
    if L < 1.0:  # meters
        raise ValueError(f"degenerate runway: ends {L:.3f} m apart")
    ux, uy = dx / L, dy / L
    ext = (HARD_SURFACE_EXT_FT * FT) if rw.paved else 0.0
    px1, py1 = x1 - ux * ext, y1 - uy * ext   # primary end at end-1 side
    px2, py2 = x2 + ux * ext, y2 + uy * ext   # primary end at end-2 side

    parts = []
    primary = _rect_from_centerline(px1, py1, px2, py2, prim_w)
    parts.append(primary)

    # Approach surfaces — one per end, pointing outward.
    def approach(px, py, ux_out, uy_out, cls):
        outer_w_ft, length_ft = APPROACH[cls]
        outer_w = outer_w_ft * FT
        length = length_ft * FT
        ex, ey = px + ux_out * length, py + uy_out * length
        return _rect_from_centerline(px, py, ex, ey, prim_w, outer_w)

    parts.append(approach(px1, py1, -ux, -uy, rw.cls1))
    parts.append(approach(px2, py2,  ux,  uy, rw.cls2))

    # Horizontal surface: arcs (radius per most-demanding end) swung from each
    # primary end center, joined by outer tangents -> stadium/oval hull.
    r_h = max(HORIZ_RADIUS_FT[rw.cls1], HORIZ_RADIUS_FT[rw.cls2]) * FT
    # angle of centerline
    base = math.atan2(uy, ux)
    # half-circle around end-2 (facing outward) + half-circle around end-1
    arc2 = _arc(px2, py2, r_h, base - math.pi / 2, base + math.pi / 2)
    arc1 = _arc(px1, py1, r_h, base + math.pi / 2, base + 3 * math.pi / 2)
    horizontal = Polygon(arc2 + arc1)
    parts.append(horizontal)

    # Conical surface: ring extending CONICAL_EXTENT_FT beyond horizontal.
    r_c = r_h + CONICAL_EXTENT_FT * FT
    arc2c = _arc(px2, py2, r_c, base - math.pi / 2, base + math.pi / 2)
    arc1c = _arc(px1, py1, r_c, base + math.pi / 2, base + 3 * math.pi / 2)
    conical = Polygon(arc2c + arc1c)
    parts.append(conical)

    # Transitional surface (7:1) 2D footprint:
    # rises from sides of primary & approach to the horizontal-surface height
    # (150 ft). Horizontal ground extent of a 7:1 surface reaching 150 ft is
    # 150*7 = 1,050 ft beyond the primary/approach edge. The conical/horizontal
    # already covers the runway area; the part that matters is alongside the
    # long approach corridors. We widen each approach by 1,050 ft on each side
    # to capture it, then let unary_union dissolve everything.
    trans_off = 150 * 7 * FT  # 1,050 ft -> m
    def approach_widened(px, py, ux_out, uy_out, cls):
        outer_w_ft, length_ft = APPROACH[cls]
        outer_w = outer_w_ft * FT + 2 * trans_off
        length = length_ft * FT
        ex, ey = px + ux_out * length, py + uy_out * length
        return _rect_from_centerline(px, py, ex, ey, prim_w + 2 * trans_off, outer_w)
    parts.append(approach_widened(px1, py1, -ux, -uy, rw.cls1))
    parts.append(approach_widened(px2, py2,  ux,  uy, rw.cls2))

    # Drop any None/empty parts and repair invalid sub-polygons before the
    # union, so unary_union doesn't choke on degenerate inputs.
    clean = []
    for p in parts:
        if p is None or p.is_empty:
            continue
        if not p.is_valid:
            p = p.buffer(0)
        if not p.is_empty:
            clean.append(p)
    if not clean:
        raise ValueError("no valid surface geometry")
    # GEOS emits harmless divide-by-zero/invalid-value FP warnings while
    # dissolving these surfaces; the result is valid. Silence the noise.
    with np.errstate(divide="ignore", invalid="ignore"):
        merged = unary_union(clean)  # dissolve all into single footprint
        if not merged.is_valid:
            merged = merged.buffer(0)

    # back to WGS84
    return shp_transform(to_wgs, merged)


if __name__ == "__main__":
    # quick smoke test: a single precision-instrument runway near Denver
    rw = RunwayEnd(
        lat1=39.8561, lon1=-104.6737,
        lat2=39.8410, lon2=-104.6737,
        cls1="PIR", cls2="PIR", paved=True, ident="TEST 17/35",
    )
    fp = build_footprint(rw)
    print("footprint type:", fp.geom_type)
    print("valid:", fp.is_valid)
    # rough area check in km^2 via an equal-area projection
    ea = CRS.from_proj4("+proj=cea +lat_ts=39.85 +datum=WGS84 +units=m")
    to_ea = Transformer.from_crs("EPSG:4326", ea, always_xy=True).transform
    print("area km^2:", round(shp_transform(to_ea, fp).area / 1e6, 1))
    print("bounds:", [round(b, 4) for b in fp.bounds])
