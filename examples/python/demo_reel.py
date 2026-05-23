"""Combined demo reel — Aizawa → Clifford → Earth on a loop.

Cycles three demos with ~1 second crossfades between them, driven by
the phosphor persistence (previous demo lingers as a fading ghost
while the next one comes in). Hershey-labeled at the bottom for the
first ~4 seconds of each.

Designed for recording one continuous video instead of stitching three.

    python demo_reel.py            # loop forever (Ctrl-C to stop)
    python demo_reel.py --once     # run through each demo once and exit
    python demo_reel.py --fast     # 3s per demo, no labels — for tuning the
                                   # phosphor setting live without waiting

Before starting, increase the display's phosphor time constant so the
crossfades are visible:

  - Open the on-screen settings panel (press `o` on the display window)
  - Drag the PHOSPHOR slider to ~0.20, then close the panel (`o`)
  - OR press `d` on the display ~10 times (each tap multiplies by 1.2)

At the shipped default (`phosphor_tc = 0.0323`) the crossfade is
invisible — previous content decays to essentially zero within ~5
frames. At 0.20 it lingers for about a second. Press `0` to reset.

The demos themselves remain runnable standalone — this file just
re-implements them as small classes that share a per-frame `update()`
contract so the reel can swap between them cleanly.
"""

import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import List, Tuple

from vector_client import Frame, VectorDisplay
import hershey


# ─────────────────────────────────────────────────────────────────────────────
# Aizawa attractor (3D continuous)
# ─────────────────────────────────────────────────────────────────────────────

AIZ_A, AIZ_B, AIZ_C, AIZ_D, AIZ_E, AIZ_F = 0.95, 0.7, 0.6, 3.5, 0.25, 0.1
AIZ_TRAIL = 1500
AIZ_STEPS_PER_FRAME = 8
AIZ_DT = 0.011
AIZ_SCALE = 0.55
AIZ_Z_OFFSET = -0.7
AIZ_ROT_RATE = 0.18
AIZ_TILT = math.radians(20.0)


def aizawa_deriv(s):
    x, y, z = s
    return (
        (z - AIZ_B) * x - AIZ_D * y,
        AIZ_D * x + (z - AIZ_B) * y,
        AIZ_C + AIZ_A * z - z * z * z / 3.0 - (x * x + y * y) * (1 + AIZ_E * z) + AIZ_F * z * x * x * x,
    )


def rk4(state, f, dt):
    k1 = f(state)
    s2 = tuple(a + dt / 2 * b for a, b in zip(state, k1))
    k2 = f(s2)
    s3 = tuple(a + dt / 2 * b for a, b in zip(state, k2))
    k3 = f(s3)
    s4 = tuple(a + dt * b for a, b in zip(state, k3))
    k4 = f(s4)
    return tuple(
        a + dt / 6 * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i]) for i, a in enumerate(state)
    )


class AizawaDemo:
    NAME = "AIZAWA ATTRACTOR"

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = (0.1, 0.0, 0.0)
        self.trail = []
        # Warm-up so the initial frames show the actual attractor, not the
        # transient as the orbit drops onto it from the seed point.
        for _ in range(400):
            self.state = rk4(self.state, aizawa_deriv, AIZ_DT)

    def update(self, t: float, dt: float) -> Frame:
        for _ in range(AIZ_STEPS_PER_FRAME):
            self.state = rk4(self.state, aizawa_deriv, AIZ_DT)
            self.trail.append(self.state)
        if len(self.trail) > AIZ_TRAIL:
            self.trail = self.trail[-AIZ_TRAIL:]

        yaw = t * AIZ_ROT_RATE
        cy, sy = math.cos(yaw), math.sin(yaw)
        ct, st = math.cos(AIZ_TILT), math.sin(AIZ_TILT)

        pts = []
        for x, y, z in self.trail:
            z_off = z + AIZ_Z_OFFSET
            # Yaw around z
            x2 = x * cy - y * sy
            y2 = x * sy + y * cy
            # Tilt around x mixing y/z
            y3 = y2 * ct - z_off * st
            z3 = y2 * st + z_off * ct
            pts.append((x2 * AIZ_SCALE, z3 * AIZ_SCALE))

        f = Frame()
        f.polyline(pts, 0.85)
        f.dot(pts[-1][0], pts[-1][1], 1.6)
        return f


# ─────────────────────────────────────────────────────────────────────────────
# Clifford attractor (2D discrete)
# ─────────────────────────────────────────────────────────────────────────────

CLIF_CENTER = (-1.4, 1.6, 1.0, 0.7)
CLIF_DRIFTS = (0.30, 0.20, 0.25, 0.20)
CLIF_DRIFT_FREQS = (0.040, 0.057, 0.071, 0.083)
CLIF_SCALE = 0.36
CLIF_INTENSITY = 1.0
CLIF_ITERS = 300


def clifford_step(x, y, a, b, c, d):
    return (math.sin(a * y) + c * math.cos(a * x), math.sin(b * x) + d * math.cos(b * y))


class CliffordDemo:
    NAME = "CLIFFORD ATTRACTOR"

    def __init__(self):
        self.reset()

    def reset(self):
        self.x, self.y = 0.1, 0.0
        for _ in range(1000):
            self.x, self.y = clifford_step(self.x, self.y, *CLIF_CENTER)

    def update(self, t: float, dt: float) -> Frame:
        a = CLIF_CENTER[0] + CLIF_DRIFTS[0] * math.sin(CLIF_DRIFT_FREQS[0] * t)
        b = CLIF_CENTER[1] + CLIF_DRIFTS[1] * math.sin(CLIF_DRIFT_FREQS[1] * t + 1.0)
        c = CLIF_CENTER[2] + CLIF_DRIFTS[2] * math.sin(CLIF_DRIFT_FREQS[2] * t + 2.0)
        d = CLIF_CENTER[3] + CLIF_DRIFTS[3] * math.sin(CLIF_DRIFT_FREQS[3] * t + 3.0)

        f = Frame()
        for _ in range(CLIF_ITERS):
            self.x, self.y = clifford_step(self.x, self.y, a, b, c, d)
            px = self.x * CLIF_SCALE
            py = self.y * CLIF_SCALE
            if -1.0 < px < 1.0 and -1.0 < py < 1.0:
                f.dot(px, py, CLIF_INTENSITY)
        return f


# ─────────────────────────────────────────────────────────────────────────────
# Rotating Earth
# ─────────────────────────────────────────────────────────────────────────────

EARTH_DATA = Path(__file__).parent / "data" / "world_coastline.json"
EARTH_SCALE = 0.78
EARTH_ROT_RATE = -0.18
EARTH_TILT = math.radians(23.4)
EARTH_VIZ_Z = -0.02
EARTH_COAST_I = 0.95
EARTH_GRID_I = 0.30
EARTH_EQUATOR_I = 0.65
EARTH_PRIME_I = 0.65


def lat_lon_to_xyz(lat_deg, lon_deg):
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    cl = math.cos(lat)
    return (cl * math.cos(lon), math.sin(lat), cl * math.sin(lon))


def rotate_y(p, c, s):
    x, y, z = p
    return (x * c + z * s, y, -x * s + z * c)


def tilt_z(p, c, s):
    x, y, z = p
    return (x * c - y * s, x * s + y * c, z)


def earth_project(p):
    # See earth.py for the rationale on the x-flip.
    return (-p[0] * EARTH_SCALE, p[1] * EARTH_SCALE)


def emit_visible(frame: Frame, pts3d, intensity: float):
    run = []
    for p in pts3d:
        if p[2] >= EARTH_VIZ_Z:
            run.append(earth_project(p))
        else:
            if len(run) >= 2:
                frame.polyline(run, intensity)
            run = []
    if len(run) >= 2:
        frame.polyline(run, intensity)


def make_graticule():
    out = []
    for lon in range(0, 360, 30):
        pts = [lat_lon_to_xyz(lat, lon) for lat in range(-90, 91, 5)]
        out.append((EARTH_PRIME_I if lon == 0 else EARTH_GRID_I, pts))
    for lat in (-60, -30, 0, 30, 60):
        pts = [lat_lon_to_xyz(lat, lon) for lon in range(0, 361, 6)]
        out.append((EARTH_EQUATOR_I if lat == 0 else EARTH_GRID_I, pts))
    return out


class EarthDemo:
    NAME = "ROTATING EARTH"

    def __init__(self):
        with open(EARTH_DATA, encoding="utf-8") as fp:
            coastlines = json.load(fp)["polylines"]
        self.coast_3d = [
            [lat_lon_to_xyz(lat, lon) for (lat, lon) in poly] for poly in coastlines
        ]
        self.graticule = make_graticule()
        self.tilt_c = math.cos(EARTH_TILT)
        self.tilt_s = math.sin(EARTH_TILT)

    def reset(self):
        # Stateless — yaw is purely a function of t. Nothing to reset.
        pass

    def update(self, t: float, dt: float) -> Frame:
        yaw = t * EARTH_ROT_RATE
        yc, ys = math.cos(yaw), math.sin(yaw)

        f = Frame()
        for intensity, pts in self.graticule:
            rotated = [tilt_z(rotate_y(p, yc, ys), self.tilt_c, self.tilt_s) for p in pts]
            emit_visible(f, rotated, intensity)
        for poly in self.coast_3d:
            rotated = [tilt_z(rotate_y(p, yc, ys), self.tilt_c, self.tilt_s) for p in poly]
            emit_visible(f, rotated, EARTH_COAST_I)
        return f


# ─────────────────────────────────────────────────────────────────────────────
# Reel
# ─────────────────────────────────────────────────────────────────────────────

# (constructor, duration_seconds). Order chosen so it cycles cleanly: ends
# on Earth, restarts on Aizawa — both visually distinct so the loop point
# isn't a jarring jump.
DEMO_SCHEDULE: List[Tuple[type, float]] = [
    (AizawaDemo,  18.0),
    (CliffordDemo, 18.0),
    (EarthDemo,    18.0),
]

# Label timing (seconds). Fade in, hold, fade out, gone.
LABEL_FADE_IN = 0.4
LABEL_HOLD = 2.4
LABEL_FADE_OUT = 1.2


def label_intensity(t_in_demo: float) -> float:
    if t_in_demo < LABEL_FADE_IN:
        return t_in_demo / LABEL_FADE_IN
    held_end = LABEL_FADE_IN + LABEL_HOLD
    if t_in_demo < held_end:
        return 1.0
    gone = held_end + LABEL_FADE_OUT
    if t_in_demo < gone:
        return 1.0 - (t_in_demo - held_end) / LABEL_FADE_OUT
    return 0.0


def banner(d: VectorDisplay, demos):
    print(f"connected; viewport {d.viewport}")
    print()
    print("For the intended crossfade between demos, increase phosphor_tc first:")
    print("  - On the display window: press 'o', drag PHOSPHOR slider to ~0.20")
    print("    (or press 'd' on the display ~10 times)")
    print("  - Press '0' to reset to shipped defaults.")
    print()
    sched = ", ".join(f"{demo.NAME} {dur:.0f}s" for demo, dur in demos)
    print(f"Schedule: {sched}")
    print("Ctrl-C to stop.")
    print()


async def main():
    once = "--once" in sys.argv
    fast = "--fast" in sys.argv

    # In tune mode, every demo runs for 3 seconds and labels are suppressed.
    # The point is to see the transition between segments often enough that
    # you can dial the phosphor slider while watching.
    if fast:
        demos = [(cls(), 3.0) for cls, _ in DEMO_SCHEDULE]
    else:
        demos = [(cls(), dur) for cls, dur in DEMO_SCHEDULE]

    async with VectorDisplay() as d:
        banner(d, demos)

        demo_idx = 0
        demos[demo_idx][0].reset()
        t_global_start = time.monotonic()
        demo_start = 0.0
        loops = 0
        print(f"-> {demos[demo_idx][0].NAME}")

        while True:
            t_global = time.monotonic() - t_global_start
            t_in_demo = t_global - demo_start

            demo, duration = demos[demo_idx]

            if t_in_demo >= duration:
                demo_idx += 1
                if demo_idx >= len(demos):
                    if once:
                        print("done")
                        return
                    demo_idx = 0
                    loops += 1
                demos[demo_idx][0].reset()
                demo_start = t_global
                t_in_demo = 0.0
                demo, duration = demos[demo_idx]
                print(f"-> {demo.NAME}" + (f" (loop {loops})" if loops else ""))

            frame = demo.update(t_in_demo, 1 / 60)

            if not fast:
                li = label_intensity(t_in_demo)
                if li > 0.01:
                    hershey.draw_string_centered(frame, demo.NAME, 0.0, -0.93, 0.038, li)

            try:
                await d.send(frame)
            except Exception as e:
                print(f"send failed: {e}")
                return

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
