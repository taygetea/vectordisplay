"""Rotating wireframe globe.

Coastlines from Natural Earth 110m (baked once by bake_world.py) plus a
graticule of meridians and parallels at every 30°. The whole thing
rotates around an axis tilted 23.4° (Earth's real obliquity) so it
doesn't look completely uniform along the spin axis.

Visibility: each (lat, lon) maps to a point on the unit sphere; after
the full rotation we keep only points on the visible hemisphere
(rotated z > 0). Polylines are split at the visibility boundary so
back-of-globe segments don't bleed onto the front.

Run:
    cargo run --release   # start the display first
    python bake_world.py  # once, to fetch coastline data
    python earth.py
"""

import asyncio
import json
import math
import time
from pathlib import Path

from vector_client import Frame, VectorDisplay


DATA_PATH = Path(__file__).parent / "data" / "world_coastline.json"

# Visual
SCALE = 0.78               # globe radius in NDC
ROT_RATE = 0.18            # rad/s around Earth's spin axis
AXIAL_TILT = math.radians(23.4)  # real
EQUATOR_INTENSITY = 0.65
PRIME_MERIDIAN_INTENSITY = 0.65
GRATICULE_INTENSITY = 0.30
COAST_INTENSITY = 0.95

# Hide points whose rotated z is below this threshold (small negative is OK
# so polylines crossing the limb don't suddenly snap — gentle wraparound).
VISIBILITY_Z = -0.02


def lat_lon_to_xyz(lat_deg, lon_deg):
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    cl = math.cos(lat)
    # Convention: y axis = spin axis (up). x and z are equatorial.
    return (
        cl * math.cos(lon),
        math.sin(lat),
        cl * math.sin(lon),
    )


def rotate_y(p, c, s):
    """Rotation around y axis by angle whose cos=c, sin=s."""
    x, y, z = p
    return (x * c + z * s, y, -x * s + z * c)


def tilt_z(p, c, s):
    """Tilt around z axis (so the spin axis no longer points straight up)."""
    x, y, z = p
    return (x * c - y * s, x * s + y * c, z)


def project(p):
    """Orthographic to NDC. y is screen-up."""
    return (p[0] * SCALE, p[1] * SCALE)


def emit_visible(frame, pts3d, intensity):
    """Split a 3D polyline at the visibility horizon (z>0 visible) and emit
    each visible run as its own polyline."""
    run = []
    for p in pts3d:
        if p[2] >= VISIBILITY_Z:
            run.append(project(p))
        else:
            if len(run) >= 2:
                frame.polyline(run, intensity)
            run = []
    if len(run) >= 2:
        frame.polyline(run, intensity)


def transform_chain(p, yaw_c, yaw_s, tilt_c, tilt_s):
    """Spin around y, then tilt around z. The tilt is applied AFTER the spin
    so the tilt direction is fixed in screen space (Earth wobbles 23.4° off
    from the screen vertical, rather than the wobble axis itself rotating)."""
    p = rotate_y(p, yaw_c, yaw_s)
    p = tilt_z(p, tilt_c, tilt_s)
    return p


def generate_graticule():
    """Returns a list of (intensity, [3d_points]) tuples for the lat/lon grid."""
    out = []
    # Meridians every 30°. Each is a half-great-circle from south pole to north.
    for lon in range(0, 360, 30):
        pts = [lat_lon_to_xyz(lat, lon) for lat in range(-90, 91, 5)]
        intensity = PRIME_MERIDIAN_INTENSITY if lon == 0 else GRATICULE_INTENSITY
        out.append((intensity, pts))
    # Parallels every 30° (skip the poles, where they degenerate).
    for lat in (-60, -30, 0, 30, 60):
        pts = [lat_lon_to_xyz(lat, lon) for lon in range(0, 361, 6)]
        intensity = EQUATOR_INTENSITY if lat == 0 else GRATICULE_INTENSITY
        out.append((intensity, pts))
    return out


async def main():
    with open(DATA_PATH, encoding="utf-8") as fp:
        coastlines = json.load(fp)["polylines"]

    print(f"loaded {len(coastlines)} coastline polylines, "
          f"{sum(len(p) for p in coastlines)} vertices total")

    # Pre-convert coastlines from (lat, lon) to 3D unit-sphere points once.
    coast_3d = [[lat_lon_to_xyz(lat, lon) for (lat, lon) in poly] for poly in coastlines]
    graticule = generate_graticule()

    tilt_c = math.cos(AXIAL_TILT)
    tilt_s = math.sin(AXIAL_TILT)

    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        t0 = time.monotonic()
        last_print = 0.0

        while True:
            t = time.monotonic() - t0
            yaw = t * ROT_RATE
            yc, ys = math.cos(yaw), math.sin(yaw)

            f = Frame()

            # Graticule first (dimmer; coastlines overdraw at the limb).
            for intensity, pts in graticule:
                rotated = [transform_chain(p, yc, ys, tilt_c, tilt_s) for p in pts]
                emit_visible(f, rotated, intensity)

            # Coastlines.
            for poly3d in coast_3d:
                rotated = [transform_chain(p, yc, ys, tilt_c, tilt_s) for p in poly3d]
                emit_visible(f, rotated, COAST_INTENSITY)

            try:
                await d.send(f)
            except Exception as e:
                print(f"send failed: {e}")
                break

            if t - last_print > 2.0:
                last_print = t
                deg = math.degrees(yaw) % 360
                print(f"t={t:6.1f}  rotation={deg:6.1f}°  bytes={len(f)}")

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
