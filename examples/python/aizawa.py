"""Aizawa attractor — 3D continuous chaotic system.

    dx/dt = (z - b)*x - d*y
    dy/dt = d*x + (z - b)*y
    dz/dt = c + a*z - z^3/3 - (x^2 + y^2)*(1 + e*z) + f*z*x^3

Shape: a vortex with a ribbon wrapped around its top, like a spinning
top wearing a scarf. Distinctly unlike Lorenz — smooth, twisting,
visibly periodic at the scale of a few seconds but never repeating.

We integrate with RK4, keep a ring buffer of recent orbit points, and
emit them every frame as one long polyline with a bright head dot.
The viewpoint precesses slowly around the vertical axis so the shape
reads from multiple angles.

Run:
    python aizawa.py
"""

import asyncio
import math
import time

from vector_client import Frame, VectorDisplay


# Parameters from the standard Aizawa attractor.
A, B, C, D, E, F = 0.95, 0.7, 0.6, 3.5, 0.25, 0.1

# Trail length (points). 1500 at 8 RK4 steps/frame at 60 fps ≈ 3 seconds
# of orbit history visible at once — long enough to see the shape, short
# enough that the tip "leads" the rest.
TRAIL_LEN = 1500
STEPS_PER_FRAME = 8
DT = 0.011

# Visual fit
SCALE = 0.55
Z_OFFSET = -0.7  # the attractor's z is roughly [0, 2]; shift to center

# Camera
ROT_RATE = 0.18  # rad/s around vertical
TILT = math.radians(20.0)  # gentle tilt so we don't look straight along the spin axis


def aizawa(state):
    x, y, z = state
    return (
        (z - B) * x - D * y,
        D * x + (z - B) * y,
        C + A * z - z * z * z / 3.0 - (x * x + y * y) * (1.0 + E * z) + F * z * x * x * x,
    )


def rk4_step(state, dt):
    k1 = aizawa(state)
    s2 = (state[0] + dt / 2 * k1[0], state[1] + dt / 2 * k1[1], state[2] + dt / 2 * k1[2])
    k2 = aizawa(s2)
    s3 = (state[0] + dt / 2 * k2[0], state[1] + dt / 2 * k2[1], state[2] + dt / 2 * k2[2])
    k3 = aizawa(s3)
    s4 = (state[0] + dt * k3[0], state[1] + dt * k3[1], state[2] + dt * k3[2])
    k4 = aizawa(s4)
    return (
        state[0] + dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]),
        state[1] + dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]),
        state[2] + dt / 6 * (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2]),
    )


def project(point, yaw, tilt):
    """Rotate around vertical (z) by yaw, then tilt the view by tilt around x,
    then orthographic-project to (x, y) in NDC."""
    x, y, z = point
    z = z + Z_OFFSET
    # Yaw around z
    cy, sy = math.cos(yaw), math.sin(yaw)
    x, y = x * cy - y * sy, x * sy + y * cy
    # Tilt around x: y and z mix
    ct, st = math.cos(tilt), math.sin(tilt)
    y, z = y * ct - z * st, y * st + z * ct
    return (x * SCALE, z * SCALE)


async def main():
    state = (0.1, 0.0, 0.0)
    trail = []

    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        t0 = time.monotonic()
        last_print = 0.0

        while True:
            t = time.monotonic() - t0

            # Integrate forward
            for _ in range(STEPS_PER_FRAME):
                state = rk4_step(state, DT)
                trail.append(state)
            if len(trail) > TRAIL_LEN:
                trail = trail[-TRAIL_LEN:]

            yaw = t * ROT_RATE

            # Project the whole trail and emit as one polyline.
            pts = [project(p, yaw, TILT) for p in trail]
            f = Frame()
            f.polyline(pts, 0.85)
            # Bright leading head
            f.dot(pts[-1][0], pts[-1][1], 1.6)

            try:
                await d.send(f)
            except Exception as e:
                print(f"send failed: {e}")
                break

            if t - last_print > 2.0:
                last_print = t
                print(f"t={t:6.1f}  trail={len(trail)}  yaw={math.degrees(yaw) % 360:6.1f}°")

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
