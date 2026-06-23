"""
make_maps.py — quick visualizations of the Part 77 exclusion footprints.

Outputs (next to the .gpkg):
  part77_overview.png   — static CONUS map, runways colored by classification
  part77_interactive.html — pan/zoom Leaflet map of the dissolved exclusion mask

Usage:
  python make_maps.py [--gpkg part77_footprints.gpkg]
"""
from __future__ import annotations
import argparse, os
import warnings

import numpy as np
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

# Color per Part 77 classification (most-demanding end shown).
CLASS_COLORS = {
    "UTIL_VIS": "#9ecae1",
    "UTIL_NPI": "#6baed6",
    "VIS":      "#74c476",
    "NPI_GT":   "#fd8d3c",
    "NPI_LOW":  "#e6550d",
    "PIR":      "#de2d26",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpkg", default="part77_footprints.gpkg")
    args = ap.parse_args()

    base = os.path.splitext(os.path.basename(args.gpkg))[0]
    outdir = os.path.dirname(os.path.abspath(args.gpkg))

    by_rwy = gpd.read_file(args.gpkg, layer="part77_by_runway")
    dissolved = gpd.read_file(args.gpkg, layer="part77_dissolved")

    # classification shown = more demanding of the two ends
    order = ["UTIL_VIS", "UTIL_NPI", "VIS", "NPI_GT", "NPI_LOW", "PIR"]
    rank = {c: i for i, c in enumerate(order)}
    by_rwy["cls"] = by_rwy.apply(
        lambda r: r["cls1"] if rank.get(r["cls1"], 0) >= rank.get(r["cls2"], 0)
        else r["cls2"], axis=1)

    # ---- static CONUS overview -------------------------------------------
    fig, ax = plt.subplots(figsize=(14, 8))
    # optional country backdrop for context
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            world = gpd.read_file(gpd.datasets.get_path("naturalearth_lowres"))
        world[world.continent == "North America"].plot(
            ax=ax, color="#f0f0f0", edgecolor="#cccccc", linewidth=0.5)
    except Exception:
        pass

    for cls, color in CLASS_COLORS.items():
        sub = by_rwy[by_rwy["cls"] == cls]
        if len(sub):
            sub.plot(ax=ax, color=color, edgecolor="none", alpha=0.7)

    ax.set_xlim(-125, -66)   # CONUS
    ax.set_ylim(24, 50)
    ax.set_title("FAA Part 77.19 imaginary-surface 2D footprints (CONUS)\n"
                 f"{len(by_rwy):,} runways — colored by classification "
                 "(AK/HI/territories present in data, off-frame)")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    legend = [Patch(facecolor=CLASS_COLORS[c], label=c) for c in order]
    ax.legend(handles=legend, loc="lower left", fontsize=8, title="Class")
    png = os.path.join(outdir, f"{base}_overview.png")
    fig.savefig(png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", png)

    # ---- interactive map (dissolved mask, simplified for browser speed) --
    try:
        import folium
        d = dissolved.copy()
        # simplify so the HTML isn't enormous (~0.003 deg ~ 300 m)
        d["geometry"] = d.geometry.simplify(0.003, preserve_topology=True)
        c = d.geometry.union_all().centroid if hasattr(d.geometry, "union_all") \
            else d.geometry.unary_union.centroid
        m = folium.Map(location=[39.5, -98.35], zoom_start=4, tiles="cartodbpositron")
        folium.GeoJson(
            d.to_json(),
            name="Part 77 exclusion mask",
            style_function=lambda f: {"fillColor": "#de2d26", "color": "#de2d26",
                                      "weight": 0.3, "fillOpacity": 0.4},
        ).add_to(m)
        folium.LayerControl().add_to(m)
        html = os.path.join(outdir, f"{base}_interactive.html")
        m.save(html)
        print("wrote", html)
    except Exception as ex:
        print("interactive map skipped:", ex)


if __name__ == "__main__":
    main()
