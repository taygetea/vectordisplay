"""One-time data bake for the wargames demo.

Downloads Natural Earth 1:50m admin-1 state boundaries (public domain),
filters to the lower-48 US states + Alaska + Hawaii, simplifies the
boundary polylines with Douglas-Peucker, projects to a unit square via
Albers Equal-Area Conic, and writes the result to data/usa.json.

Run once from this directory:

    python bake_data.py

The output is committed to the repo; you do not need to run this unless
you want to re-bake with different parameters.
"""

import json
import math
import urllib.request
from pathlib import Path

# Natural Earth 1:50m admin-1 (states/provinces) mirrored as GeoJSON.
SOURCE = (
    "https://raw.githubusercontent.com/martynafford/natural-earth-geojson/"
    "master/50m/cultural/ne_50m_admin_1_states_provinces.json"
)
LAKES_SOURCE = (
    "https://raw.githubusercontent.com/martynafford/natural-earth-geojson/"
    "master/50m/physical/ne_50m_lakes.json"
)

# Lake names to include (matches names in ne_50m_lakes — formats vary).
GREAT_LAKE_NAMES = {
    "Lake Superior",
    "Lake Michigan",
    "Lake Huron",
    "L. Erie",
    "L. Ontario",
    "L. St. Clair",
    "Great Salt Lake",
}

# Albers Equal-Area Conic parameters tuned for the conterminous US.
# These are the standard "USA Contiguous Albers" parallels used in atlases.
ALBERS_LAT0 = 39.5     # latitude of origin
ALBERS_LON0 = -98.0    # central meridian
ALBERS_LAT1 = 29.5     # standard parallel 1
ALBERS_LAT2 = 45.5     # standard parallel 2

# Douglas-Peucker simplification tolerance, in projection units (post-Albers).
# Higher = fewer points / chunkier. The vector display will smooth over
# small steps anyway, so tolerance can be fairly loose.
SIMPLIFY_TOLERANCE = 0.0015

# Final fit into NDC. The lower-48 should fill ~95% of the horizontal range
# and roughly half the vertical (the US is wider than it is tall).
NDC_TARGET_X = 0.92    # half-width: US fits in [-NDC_TARGET_X, +NDC_TARGET_X]
NDC_TARGET_Y = 0.70    # half-height after centering, leaving room for HUD top/bottom


def albers(lat_deg: float, lon_deg: float):
    """Project (lat, lon) → (x, y) via Albers Equal-Area Conic.

    See Snyder, "Map Projections — A Working Manual", USGS PP 1395, §14.
    Outputs are in units of Earth radii (unitless if radius=1).
    """
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    lat0 = math.radians(ALBERS_LAT0)
    lon0 = math.radians(ALBERS_LON0)
    lat1 = math.radians(ALBERS_LAT1)
    lat2 = math.radians(ALBERS_LAT2)

    n = 0.5 * (math.sin(lat1) + math.sin(lat2))
    c = math.cos(lat1) ** 2 + 2 * n * math.sin(lat1)
    rho = math.sqrt(c - 2 * n * math.sin(lat)) / n
    rho0 = math.sqrt(c - 2 * n * math.sin(lat0)) / n
    theta = n * (lon - lon0)

    x = rho * math.sin(theta)
    y = rho0 - rho * math.cos(theta)
    return x, y


def _fit_transform(points):
    """Compute (scale, ox, oy) that fits `points` into the NDC target box,
    preserving aspect. Alaska/Hawaii inflate the bounding box wildly; use
    only lower-48 points for fitting (caller is responsible for the filter).
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x
    span_y = max_y - min_y
    scale = min((2 * NDC_TARGET_X) / span_x, (2 * NDC_TARGET_Y) / span_y)
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2
    return scale, cx, cy


def apply_transform(x: float, y: float, scale: float, cx: float, cy: float):
    return ((x - cx) * scale, (y - cy) * scale)


def perpendicular_distance(p, a, b):
    """Distance from p to line segment ab."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy)


def douglas_peucker(points, epsilon):
    """Simplify a polyline. Returns a list with a subset of the input points."""
    if len(points) < 3:
        return list(points)
    # Find the point with the max distance from the chord (start, end)
    start, end = points[0], points[-1]
    max_d, max_i = 0.0, 0
    for i in range(1, len(points) - 1):
        d = perpendicular_distance(points[i], start, end)
        if d > max_d:
            max_d, max_i = d, i
    if max_d > epsilon:
        left = douglas_peucker(points[: max_i + 1], epsilon)
        right = douglas_peucker(points[max_i:], epsilon)
        return left[:-1] + right
    return [start, end]


# US states to include (Natural Earth uses the postal-code "iso_3166_2"
# field; filter on country code first for safety).
INCLUDE_COUNTRY = "USA"
EXCLUDE_REGIONS = set()  # could exclude AK/HI here; we keep them


def project_ring_raw(ring):
    """Project a GeoJSON [[lon, lat], ...] ring through Albers only.
    Returns list of (x,y) in raw Albers space (Earth-radius units)."""
    return [albers(lat, lon) for (lon, lat) in ring]


# States we drop from the output entirely. Alaska scaled up 2.5× to fit
# CONUS would spill into the corner of the display; Hawaii does the same
# off the bottom-left. Real WarGames-style maps either omit them or
# show small insets — we just omit.
DROP_STATES = {"Alaska", "Hawaii"}


def main():
    out_path = Path(__file__).parent / "data" / "usa.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {SOURCE} ...")
    with urllib.request.urlopen(SOURCE) as resp:
        data = json.load(resp)

    print(f"got {len(data['features'])} features")

    # First pass: project every state polygon into Albers space, drop AK/HI.
    raw_state_polylines = []
    states_seen = 0

    for feat in data["features"]:
        props = feat["properties"]
        country = props.get("adm0_a3") or props.get("iso_a2") or props.get("admin")
        if country not in ("USA", "US", "United States of America"):
            continue
        name = props.get("name") or props.get("postal") or ""
        if name in EXCLUDE_REGIONS or name in DROP_STATES:
            continue
        states_seen += 1

        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            polygons = [geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            polygons = geom["coordinates"]
        else:
            continue

        for poly in polygons:
            for ring in poly:
                pts = project_ring_raw(ring)
                simplified = douglas_peucker(pts, SIMPLIFY_TOLERANCE)
                if len(simplified) < 4:
                    continue
                raw_state_polylines.append(simplified)

    # Second pass: fetch lakes, filter to the Great Lakes set, simplify.
    print(f"Downloading {LAKES_SOURCE} ...")
    with urllib.request.urlopen(LAKES_SOURCE) as resp:
        lakes_data = json.load(resp)
    raw_lake_polylines = []
    lakes_found = []
    for feat in lakes_data["features"]:
        name = feat["properties"].get("name") or ""
        if name not in GREAT_LAKE_NAMES:
            continue
        lakes_found.append(name)
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            polygons = [geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            polygons = geom["coordinates"]
        else:
            continue
        for poly in polygons:
            for ring in poly:
                pts = project_ring_raw(ring)
                simplified = douglas_peucker(pts, SIMPLIFY_TOLERANCE)
                if len(simplified) < 4:
                    continue
                raw_lake_polylines.append(simplified)
    print(f"lakes included: {lakes_found}")

    # Compute fit from state outlines only; lakes get the same transform so
    # they line up with state coastlines.
    fit_points = [pt for pts in raw_state_polylines for pt in pts]
    if not fit_points:
        raise SystemExit("No CONUS points found — Natural Earth schema changed?")
    scale, cx, cy = _fit_transform(fit_points)
    print(f"fit transform: scale={scale:.3f}, center=({cx:.3f},{cy:.3f})")

    state_polylines = [
        [apply_transform(x, y, scale, cx, cy) for (x, y) in pts]
        for pts in raw_state_polylines
    ]
    lake_polylines = [
        [apply_transform(x, y, scale, cx, cy) for (x, y) in pts]
        for pts in raw_lake_polylines
    ]
    total_state_pts = sum(len(p) for p in state_polylines)
    total_lake_pts = sum(len(p) for p in lake_polylines)
    print(f"{states_seen} states kept, {len(state_polylines)} polylines, "
          f"{total_state_pts} vertices")
    print(f"{len(lake_polylines)} lake polylines, {total_lake_pts} vertices")

    # Major US cities (hand-curated; lat, lon). Targets and silos.
    cities = [
        ("NEW YORK",     40.7128,  -74.0060, "target"),
        ("LOS ANGELES",  34.0522, -118.2437, "target"),
        ("CHICAGO",      41.8781,  -87.6298, "target"),
        ("HOUSTON",      29.7604,  -95.3698, "target"),
        ("WASHINGTON",   38.9072,  -77.0369, "target"),
        ("SEATTLE",      47.6062, -122.3321, "target"),
        ("DENVER",       39.7392, -104.9903, "target"),
        ("MIAMI",        25.7617,  -80.1918, "target"),
        ("ATLANTA",      33.7490,  -84.3880, "target"),
        ("BOSTON",       42.3601,  -71.0589, "target"),
        ("DALLAS",       32.7767,  -96.7970, "target"),
        ("SAN FRANCISCO", 37.7749,-122.4194, "target"),
        ("DETROIT",      42.3314,  -83.0458, "target"),
        ("PHOENIX",      33.4484, -112.0740, "target"),
        ("MINOT AFB",    48.4158, -101.3577, "silo"),
        ("MALMSTROM AFB", 47.5050,-111.1850, "silo"),
        ("F.E. WARREN",  41.1499, -104.8674, "silo"),
        ("WHITEMAN AFB", 38.7300,  -93.5478, "silo"),
        ("CHEYENNE MTN", 38.7406, -104.8474, "silo"),
        ("OFFUTT AFB",   41.1180,  -95.9128, "silo"),
    ]
    projected_cities = []
    for name, lat, lon, role in cities:
        x, y = albers(lat, lon)
        nx, ny = apply_transform(x, y, scale, cx, cy)
        projected_cities.append({
            "name": name, "x": nx, "y": ny, "lat": lat, "lon": lon, "role": role,
        })

    # Adversary launch sites — actual / historical ICBM bases and launch
    # centers. Each gives a visually distinct attack vector when great-
    # circled to a US target (polar from Russia, trans-Arctic from China,
    # over-the-Pacific from DPRK). Pure geography, no opinion implied.
    launch_sites = [
        ("PLESETSK",      62.957,  40.583, "RUS"),  # Arctic launch base
        ("TATISHCHEVO",   51.660,  45.620, "RUS"),  # SS-19/27 silos
        ("YASNY",         51.092,  59.851, "RUS"),  # Dombarovsky silos
        ("KOZELSK",       54.040,  35.785, "RUS"),  # west of Moscow
        ("UZHUR",         55.300,  90.000, "RUS"),  # Siberia
        ("JIUQUAN",       40.958, 100.292, "CHN"),  # main launch center
        ("HAMI",          41.250,  93.500, "CHN"),  # newer silo field
        ("LOP NUR",       40.700,  89.500, "CHN"),  # historic test site
        ("PYONGYANG",     39.033, 125.753, "PRK"),
        ("SOHAE",         39.660, 124.710, "PRK"),  # Tongchang-ri launch
    ]
    projected_sites = []
    for name, lat, lon, country in launch_sites:
        # Project too — these mostly land off-screen, which is fine; the
        # arcs from them sweep into view as the missile gets closer.
        x, y = albers(lat, lon)
        nx, ny = apply_transform(x, y, scale, cx, cy)
        projected_sites.append({
            "name": name, "lat": lat, "lon": lon, "x": nx, "y": ny, "country": country,
        })

    output = {
        "comment": "Baked from Natural Earth 50m admin-1 + lakes, Albers Equal-Area, simplified.",
        # Projection parameters: enough for a client to re-project any
        # arbitrary (lat, lon) into the same NDC space the polylines live in.
        # Apply Albers with (lat0, lon0, lat1, lat2), then transform with
        # (x - center_x) * scale, (y - center_y) * scale.
        "projection": {
            "kind": "albers_then_fit",
            "lat0": ALBERS_LAT0,
            "lon0": ALBERS_LON0,
            "lat1": ALBERS_LAT1,
            "lat2": ALBERS_LAT2,
            "scale": scale,
            "center_x": cx,
            "center_y": cy,
        },
        "states": state_polylines,
        "lakes": lake_polylines,
        "cities": projected_cities,
        "launch_sites": projected_sites,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, separators=(",", ":"))
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
