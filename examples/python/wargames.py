"""WarGames-style US map driven over WebSocket.

US state outlines + Great Lakes from Natural Earth (baked once by
bake_data.py). Cities as crosses. Incoming missiles arc on real great-
circle paths from plausible adversary launch sites (Plesetsk, Tatishchevo,
Jiuquan, Pyongyang, etc.) projected through the same Albers projection
as the rest of the map.

Run the display first:
    cargo run --release
Then:
    python wargames.py

Click anywhere to launch a counter-attack from a random US silo to a
random adversary site. Space toggles a faster ambient mode. R resets.
"""

import asyncio
import json
import math
import random
import time
from pathlib import Path

from vector_client import Frame, VectorDisplay
import hershey


DATA_PATH = Path(__file__).parent / "data" / "usa.json"


def load_map():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


# --- Projection (replicates bake_data.py's Albers + fit transform) ---

def make_projector(p):
    """Return a function (lat_deg, lon_deg) → (x_ndc, y_ndc) matching the
    Albers projection that was used to bake the static map data."""
    lat0 = math.radians(p["lat0"])
    lon0 = math.radians(p["lon0"])
    lat1 = math.radians(p["lat1"])
    lat2 = math.radians(p["lat2"])
    n = 0.5 * (math.sin(lat1) + math.sin(lat2))
    c = math.cos(lat1) ** 2 + 2 * n * math.sin(lat1)
    rho0 = math.sqrt(c - 2 * n * math.sin(lat0)) / n
    scale = p["scale"]
    cx = p["center_x"]
    cy = p["center_y"]

    def project(lat_deg, lon_deg):
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        # Wrap longitude difference into [-pi, pi] so points on the far side
        # of the world don't go through theta = 2pi.
        delta = lon - lon0
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        rho_sq = c - 2 * n * math.sin(lat)
        # Far enough off the projection it stops being valid; clamp.
        rho = math.sqrt(max(rho_sq, 0.0)) / n
        theta = n * delta
        x = rho * math.sin(theta)
        y = rho0 - rho * math.cos(theta)
        return ((x - cx) * scale, (y - cy) * scale)

    return project


# --- Great circle math ---

def latlon_to_unit(lat_deg, lon_deg):
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    return (
        math.cos(lat) * math.cos(lon),
        math.cos(lat) * math.sin(lon),
        math.sin(lat),
    )


def unit_to_latlon(v):
    x, y, z = v
    z = max(-1.0, min(1.0, z))
    return math.degrees(math.asin(z)), math.degrees(math.atan2(y, x))


def slerp(v1, v2, t):
    """Spherical linear interpolation between two unit vectors at t ∈ [0, 1]."""
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]))
    omega = math.acos(dot)
    if omega < 1e-6:
        return v1
    so = math.sin(omega)
    a = math.sin((1 - t) * omega) / so
    b = math.sin(t * omega) / so
    return (a * v1[0] + b * v2[0], a * v1[1] + b * v2[1], a * v1[2] + b * v2[2])


def great_circle_points(start_ll, end_ll, project, progress, segments=48):
    """Sample the great circle from start_ll to end_ll, returning the portion
    up to `progress` as projected NDC polyline points. Density is constant
    along the arc (segments-per-arc samples regardless of progress)."""
    v1 = latlon_to_unit(*start_ll)
    v2 = latlon_to_unit(*end_ll)
    n_drawn = max(2, int(segments * progress) + 1)
    pts = []
    for i in range(n_drawn):
        t = min(i / segments, progress)
        v = slerp(v1, v2, t)
        lat, lon = unit_to_latlon(v)
        pts.append(project(lat, lon))
    # Pin the head exactly at progress (subsample rounding can leave it short).
    if n_drawn >= 2 and (n_drawn - 1) / segments < progress:
        v_head = slerp(v1, v2, progress)
        lat_h, lon_h = unit_to_latlon(v_head)
        pts[-1] = project(lat_h, lon_h)
    return pts


# --- Game state ---

class Missile:
    __slots__ = ("start_ll", "end_ll", "impact_xy", "t_start", "duration", "label", "incoming")

    def __init__(self, start_ll, end_ll, project, t_start, duration, label="", incoming=False):
        self.start_ll = start_ll
        self.end_ll = end_ll
        self.impact_xy = project(*end_ll)
        self.t_start = t_start
        self.duration = duration
        self.label = label
        self.incoming = incoming

    def progress(self, now):
        return min(1.0, max(0.0, (now - self.t_start) / self.duration))

    def done(self, now):
        return now - self.t_start >= self.duration


class Explosion:
    __slots__ = ("center", "t_start", "duration")

    def __init__(self, center, t_start, duration=1.4):
        self.center = center
        self.t_start = t_start
        self.duration = duration

    def progress(self, now):
        return min(1.0, max(0.0, (now - self.t_start) / self.duration))

    def done(self, now):
        return now - self.t_start >= self.duration


# --- Rendering ---

def draw_map(frame, states, lakes, intensity=0.45, lake_intensity=0.32):
    for ring in states:
        frame.polyline([(p[0], p[1]) for p in ring], intensity)
    for ring in lakes:
        frame.polyline([(p[0], p[1]) for p in ring], lake_intensity)


def draw_city(frame, x, y, role, scale=0.012):
    """Silos get a +, targets get an ×."""
    if role == "silo":
        frame.line(x - scale, y, x + scale, y, 1.0)
        frame.line(x, y - scale, x, y + scale, 1.0)
    else:
        frame.line(x - scale, y - scale, x + scale, y + scale, 0.7)
        frame.line(x - scale, y + scale, x + scale, y - scale, 0.7)


def draw_missile(frame, m: Missile, now: float, project, segments=48):
    progress = m.progress(now)
    if progress <= 0:
        return
    pts = great_circle_points(m.start_ll, m.end_ll, project, progress, segments)
    base = 0.8 if m.incoming else 1.0
    frame.polyline(pts, base)
    head = pts[-1]
    # Cull head dot if it's wildly off-screen — keeps the bright dot from
    # being a wasted command when the missile is still over the Arctic.
    if -1.2 < head[0] < 1.2 and -1.2 < head[1] < 1.2:
        frame.dot(head[0], head[1], 1.7 if m.incoming else 1.4)


def draw_explosion(frame, e: Explosion, now: float, segments=20):
    cx, cy = e.center
    # Skip explosions far off-screen (counter-strikes against adversary
    # sites detonate over Russia/China/DPRK and aren't visible).
    if not (-1.1 < cx < 1.1 and -1.1 < cy < 1.1):
        return
    p = e.progress(now)
    if p <= 0:
        return
    radius = 0.005 + 0.10 * (1 - (1 - p) ** 2)
    intensity = max(0.0, 1.4 * (1 - p))
    pts = []
    for i in range(segments + 1):
        a = (i / segments) * 2 * math.pi
        pts.append((cx + math.cos(a) * radius, cy + math.sin(a) * radius))
    frame.polyline(pts, intensity)


def draw_crosshair(frame, x, y, size=0.025, intensity=0.8):
    g = size * 0.4
    frame.line(x - size, y, x - g, y, intensity)
    frame.line(x + g, y, x + size, y, intensity)
    frame.line(x, y - size, x, y - g, intensity)
    frame.line(x, y + g, x, y + size, intensity)


# --- Helpers ---

def nearest_city(cities, point):
    best, best_d = None, float("inf")
    for c in cities:
        d = math.hypot(c["x"] - point[0], c["y"] - point[1])
        if d < best_d:
            best, best_d = c, d
    return best


def random_arc_duration(start_ll, end_ll, base=1.8, per_radian=2.4):
    """Long arcs take longer to fly. Same idea as real ICBM time-to-target."""
    v1 = latlon_to_unit(*start_ll)
    v2 = latlon_to_unit(*end_ll)
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]))
    angle = math.acos(dot)
    return base + angle * per_radian + random.uniform(-0.2, 0.4)


async def main():
    map_data = load_map()
    project = make_projector(map_data["projection"])

    states = map_data["states"]
    lakes = map_data.get("lakes", [])
    cities = map_data["cities"]
    silos = [c for c in cities if c["role"] == "silo"]
    targets = [c for c in cities if c["role"] == "target"]
    launch_sites = map_data["launch_sites"]

    missiles = []
    explosions = []
    cursor = (0.0, 0.0)
    attack_mode = False
    score_launched = 0
    score_incoming = 0

    next_ambient = 0.0

    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        print(f"launch sites: {[s['name'] for s in launch_sites]}")
        t0 = time.monotonic()
        last_print = 0.0

        while True:
            now = time.monotonic() - t0

            # --- Input events ---
            for evt in d.drain_events():
                et = evt.get("type")
                if et == "cursor_move":
                    cursor = (evt["x"], evt["y"])
                elif et == "mouse_button" and evt["button"] == "left" and evt["pressed"]:
                    # Click = launch counter-attack from random silo to a
                    # random adversary site. The click position itself
                    # doesn't pick the target (all enemies are off-screen).
                    silo = random.choice(silos)
                    enemy = random.choice(launch_sites)
                    start_ll = (silo["lat"], silo["lon"])
                    end_ll = (enemy["lat"], enemy["lon"])
                    missiles.append(Missile(
                        start_ll, end_ll, project, now,
                        random_arc_duration(start_ll, end_ll),
                        label=enemy["name"], incoming=False,
                    ))
                    score_launched += 1
                elif et == "key" and evt["pressed"]:
                    if evt["key"] == "Space":
                        attack_mode = not attack_mode
                    elif evt["key"] == "r":
                        missiles.clear()
                        explosions.clear()
                        score_launched = 0
                        score_incoming = 0

            # --- Ambient missile generator ---
            if now >= next_ambient:
                interval = 0.6 if attack_mode else 3.0
                next_ambient = now + interval * random.uniform(0.5, 1.5)
                if random.random() < (0.85 if attack_mode else 0.7):
                    # Incoming attack from a foreign launch site to a US city
                    src = random.choice(launch_sites)
                    dst = random.choice(targets)
                    start_ll = (src["lat"], src["lon"])
                    end_ll = (dst["lat"], dst["lon"])
                    missiles.append(Missile(
                        start_ll, end_ll, project, now,
                        random_arc_duration(start_ll, end_ll),
                        label=f"{src['name']}->{dst['name']}", incoming=True,
                    ))
                else:
                    # Counter-strike: silo to an adversary site
                    silo = random.choice(silos)
                    enemy = random.choice(launch_sites)
                    start_ll = (silo["lat"], silo["lon"])
                    end_ll = (enemy["lat"], enemy["lon"])
                    missiles.append(Missile(
                        start_ll, end_ll, project, now,
                        random_arc_duration(start_ll, end_ll),
                        label=f"{silo['name']}->{enemy['name']}", incoming=False,
                    ))

            # --- Detonate finished missiles ---
            still_active = []
            for m in missiles:
                if m.done(now):
                    explosions.append(Explosion(m.impact_xy, m.t_start + m.duration))
                    if m.incoming:
                        score_incoming += 1
                else:
                    still_active.append(m)
            missiles = still_active
            explosions = [e for e in explosions if not e.done(now)]

            # --- Build frame ---
            f = Frame()
            draw_map(f, states, lakes, intensity=0.45, lake_intensity=0.32)
            for c in cities:
                draw_city(f, c["x"], c["y"], c["role"])
            for m in missiles:
                draw_missile(f, m, now, project)
            for e in explosions:
                draw_explosion(f, e, now)

            cx, cy = cursor
            if -1 < cx < 1 and -1 < cy < 1:
                draw_crosshair(f, cx, cy)

            # HUD
            hershey.draw_string(f, "STRATEGIC AIR COMMAND", -0.95, 0.92, 0.028, 0.9)
            score_text = f"OUT:{score_launched:03d}  IN:{score_incoming:03d}"
            score_w = hershey.text_width(score_text, 0.028)
            hershey.draw_string(f, score_text, 0.95 - score_w, 0.92, 0.028, 0.9)
            mode_text = "ATTACK MODE ON" if attack_mode else "CLICK TO LAUNCH   SPACE FOR ATTACK"
            hershey.draw_string_centered(f, mode_text, 0.0, -0.95, 0.026, 0.7)

            try:
                await d.send(f)
            except Exception as ex:
                print(f"send failed: {ex}")
                break

            if now - last_print > 2.0:
                last_print = now
                print(f"t={now:6.1f}  missiles={len(missiles):2d}  "
                      f"explosions={len(explosions):2d}  attack={attack_mode}  "
                      f"out={score_launched} in={score_incoming}")

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
