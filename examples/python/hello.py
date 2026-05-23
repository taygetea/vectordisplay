"""Simplest possible client: rotating square. Press Esc on the display to quit.

Run the display first:
    cargo run --release
Then:
    python hello.py
"""

import asyncio
import math

from vector_client import Frame, VectorDisplay


async def main():
    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        t0 = asyncio.get_event_loop().time()
        while True:
            t = asyncio.get_event_loop().time() - t0
            angle = t * 0.5
            cos, sin = math.cos(angle), math.sin(angle)

            # Aspect-correct so the square looks square in any window size
            ax = 1.0 / d.aspect if d.aspect > 1 else 1.0
            ay = d.aspect if d.aspect < 1 else 1.0

            r = 0.6
            corners = []
            for cx, cy in [(-r, -r), (r, -r), (r, r), (-r, r)]:
                # Rotate around origin
                rx = cx * cos - cy * sin
                ry = cx * sin + cy * cos
                corners.append((rx * ax, ry * ay))

            f = Frame()
            f.polyline(corners + [corners[0]])

            try:
                await d.send(f)
            except Exception as e:
                print(f"send failed: {e}")
                break

            # Drain any input events
            for evt in d.drain_events():
                print(evt)

            await asyncio.sleep(1 / 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
