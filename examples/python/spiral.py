"""Animated spiral that walks outward then unwinds. Demonstrates intensity
variation and how the phosphor trails read with a continuously moving beam.
"""

import asyncio
import math

from vector_client import Frame, VectorDisplay


async def main():
    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        t0 = asyncio.get_event_loop().time()

        # Use fewer turns + more points-per-turn so each segment is short
        # (shorter = brighter, per the beam-time-on-target model).
        turns = 5
        points_per_turn = 96
        total = turns * points_per_turn

        while True:
            t = asyncio.get_event_loop().time() - t0
            phase = 0.5 * (1 + math.sin(t * 0.6))  # 0..1 in/out

            ax = min(1.0 / d.aspect, 1.0)
            ay = min(d.aspect, 1.0)

            f = Frame()
            pts = []
            for i in range(total + 1):
                u = i / total
                theta = u * turns * 2 * math.pi + t * 0.5
                radius = 0.85 * u ** (0.7 + 0.6 * phase)
                pts.append((math.cos(theta) * radius * ax, math.sin(theta) * radius * ay))
            f.polyline(pts)

            try:
                await d.send(f)
            except Exception as e:
                print(f"send failed: {e}")
                break

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
