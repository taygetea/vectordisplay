"""One-time bake of Natural Earth 110m coastline data into a (lat, lon)
polyline JSON that the earth.py demo can re-project per frame.

Output: data/world_coastline.json
  {
    "polylines": [
      [[lat, lon], [lat, lon], ...],
      ...
    ]
  }

We use 110m (the lowest-resolution variant) on purpose: a wireframe
globe wants chunky outlines, not photorealistic coastlines. 110m fits
on screen with room to spare and renders ~600 line segments after
trivial simplification — well under the display's instance budget.
"""

import json
import math
import urllib.request
from pathlib import Path

SOURCE = (
    "https://raw.githubusercontent.com/martynafford/natural-earth-geojson/"
    "master/110m/physical/ne_110m_coastline.json"
)

# Drop points closer than this many degrees from the previous kept point.
# Coarse; for a wireframe globe it's plenty.
ANGULAR_TOLERANCE_DEG = 0.6


def simplify(points, tol):
    """Distance-based simplification (keep every point that's farther than
    `tol` from the previous kept one). Cheap, good enough for coastlines
    that already start coarse at 110m."""
    if not points:
        return points
    out = [points[0]]
    for p in points[1:]:
        prev = out[-1]
        d = math.hypot(p[0] - prev[0], p[1] - prev[1])
        if d >= tol:
            out.append(p)
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def main():
    out_path = Path(__file__).parent / "data" / "world_coastline.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {SOURCE} ...")
    with urllib.request.urlopen(SOURCE) as resp:
        data = json.load(resp)

    print(f"got {len(data['features'])} features")

    polylines = []
    total_in = 0
    total_out = 0
    for feat in data["features"]:
        geom = feat["geometry"]
        if geom["type"] == "LineString":
            lines = [geom["coordinates"]]
        elif geom["type"] == "MultiLineString":
            lines = geom["coordinates"]
        else:
            continue
        for line in lines:
            # GeoJSON is (lon, lat) — convert to (lat, lon) so projection is obvious.
            latlon = [(pt[1], pt[0]) for pt in line]
            total_in += len(latlon)
            simplified = simplify(latlon, ANGULAR_TOLERANCE_DEG)
            if len(simplified) >= 2:
                polylines.append(simplified)
                total_out += len(simplified)

    print(f"{len(polylines)} polylines, {total_in} -> {total_out} vertices "
          f"({100 * total_out // max(1, total_in)}%)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"polylines": polylines}, f, separators=(",", ":"))
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
