"""View an SVG file (or stdin) on the vector display.

    python svg_view.py path/to/file.svg
    cat drawing.svg | python svg_view.py -

Reads the file once, parses to polylines, and pushes the same frame to
the display 60 times a second so persistence stays lit.
"""

import asyncio
import sys
from pathlib import Path

from svg import svg_to_polylines
from vector_client import Frame, VectorDisplay


def build_frame(svg_text: str) -> Frame:
    polylines = svg_to_polylines(svg_text)
    f = Frame()
    for poly in polylines:
        if len(poly) >= 2:
            f.polyline(poly, 1.0)
    return f


async def main(svg_text: str):
    frame = build_frame(svg_text)
    print(f"parsed: {len(frame)} bytes of beam commands")
    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        while True:
            try:
                await d.send(frame)
            except Exception as e:
                print(f"send failed: {e}")
                break
            await asyncio.sleep(1 / 60)


def load_svg(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    return Path(arg).read_text(encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: svg_view.py <file.svg | ->", file=sys.stderr)
        sys.exit(2)
    text = load_svg(sys.argv[1])
    try:
        asyncio.run(main(text))
    except KeyboardInterrupt:
        pass
