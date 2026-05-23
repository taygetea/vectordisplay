"""Clifford attractor — discrete 2D iteration. Looks like fractal smoke.

    x' = sin(a*y) + c*cos(a*x)
    y' = sin(b*x) + d*cos(b*y)

We iterate one trajectory many times per frame and render each iterate
as a dot. The phosphor's persistence accumulates the attractor shape
across frames; over a few seconds the structure fills in. The (a, b, c, d)
parameters drift slowly so the shape morphs continuously — different
attractors, smoothly connected.

Run:
    python clifford.py
"""

import asyncio
import math
import time

from vector_client import Frame, VectorDisplay


# Center parameter cluster + drift amplitudes. These four constants
# describe a slow Lissajous-like wander through parameter space,
# centered on a regime that always produces an interesting attractor.
CENTER = (-1.4, 1.6, 1.0, 0.7)
DRIFTS = (0.30, 0.20, 0.25, 0.20)
DRIFT_FREQS = (0.040, 0.057, 0.071, 0.083)

# How tightly to fit the attractor into NDC. Clifford with these params
# lives in roughly [-2.5, 2.5] on each axis.
SCALE = 0.36
INTENSITY = 0.55  # dots are dim individually; persistence integrates them

# Iterates per frame. The display caps line instances at 8192 — leave
# headroom so the demo never silently truncates.
ITERS_PER_FRAME = 7500


def warm_up(x, y, params, n):
    """Discard the initial transient before the trajectory lands on the attractor."""
    a, b, c, d = params
    for _ in range(n):
        x, y = math.sin(a * y) + c * math.cos(a * x), math.sin(b * x) + d * math.cos(b * y)
    return x, y


async def main():
    x, y = 0.1, 0.0
    x, y = warm_up(x, y, CENTER, 1000)

    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        t0 = time.monotonic()
        last_print = 0.0

        while True:
            t = time.monotonic() - t0

            # Slow parameter drift — every parameter on its own slow sine.
            a = CENTER[0] + DRIFTS[0] * math.sin(DRIFT_FREQS[0] * t)
            b = CENTER[1] + DRIFTS[1] * math.sin(DRIFT_FREQS[1] * t + 1.0)
            c = CENTER[2] + DRIFTS[2] * math.sin(DRIFT_FREQS[2] * t + 2.0)
            d_p = CENTER[3] + DRIFTS[3] * math.sin(DRIFT_FREQS[3] * t + 3.0)

            f = Frame()
            for _ in range(ITERS_PER_FRAME):
                # Standard Clifford step
                nx = math.sin(a * y) + c * math.cos(a * x)
                ny = math.sin(b * x) + d_p * math.cos(b * y)
                x, y = nx, ny
                px, py = x * SCALE, y * SCALE
                if -1.0 < px < 1.0 and -1.0 < py < 1.0:
                    f.dot(px, py, INTENSITY)

            try:
                await d.send(f)
            except Exception as e:
                print(f"send failed: {e}")
                break

            if t - last_print > 2.0:
                last_print = t
                print(f"t={t:6.1f}  a={a:+.3f} b={b:+.3f} c={c:+.3f} d={d_p:+.3f}")

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
