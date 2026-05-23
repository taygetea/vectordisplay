"""Extract the subset of the Hershey futural ("sans 1-stroke") font needed
for the wargames overlay, and emit it as a Python literal.

Run once. The output gets pasted into hershey.py — there's no need to keep
this script's output in version control beyond that.
"""

import json
import re
import urllib.request


SOURCE = "https://raw.githubusercontent.com/techninja/hersheytextjs/master/hersheytext.json"

# ASCII printable characters we want. Skip the lower-case letters; the
# Wargames look is uppercase. Punctuation is conservative.
WANT = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.,:-/'!"


def parse_path(d: str):
    """Convert an SVG-ish path ('M x,y L x,y x,y M x,y L x,y') into a list of
    polylines, each polyline = list of (x, y) tuples."""
    tokens = re.findall(r"[ML]|-?\d+", d)
    polylines = []
    current = []
    i = 0
    mode = None
    while i < len(tokens):
        t = tokens[i]
        if t in ("M", "L"):
            if t == "M":
                if current:
                    polylines.append(current)
                current = []
            mode = t
            i += 1
        else:
            x = int(tokens[i])
            y = int(tokens[i + 1])
            current.append((x, y))
            i += 2
    if current:
        polylines.append(current)
    # Drop polylines of length < 2 (lone moves with no draw).
    return [p for p in polylines if len(p) >= 2]


def main():
    print(f"# Downloading {SOURCE} ...")
    with urllib.request.urlopen(SOURCE) as resp:
        data = json.load(resp)

    font = data["futural"]
    chars = font["chars"]

    # Build a mapping ASCII -> (advance, polylines).
    #
    # The hersheytextjs "o" field is the original Hershey advance, but it's
    # often narrower than the actual stroke bounds (the font was designed
    # for hand-tuned kerning that overlaps glyphs). For our purposes we
    # want non-overlapping characters, so compute advance from the actual
    # x-extent of each glyph plus a small padding.
    PADDING = 4  # units between adjacent characters
    char_data = {}
    for idx, entry in enumerate(chars):
        ch = chr(33 + idx)
        if ch not in WANT:
            continue
        polys = parse_path(entry["d"])
        if polys:
            max_x = max(x for poly in polys for (x, _) in poly)
            min_x = min(x for poly in polys for (x, _) in poly)
            advance = max_x - min_x + PADDING
            # Re-anchor strokes so each glyph starts at x=0.
            polys = [[(x - min_x, y) for (x, y) in poly] for poly in polys]
        else:
            advance = entry["o"]
        char_data[ch] = (advance, polys)

    # Space is special — not in the chars list. Fixed-width.
    char_data[" "] = (12, [])

    print("# Pasted output below into hershey.py:")
    print("FONT = {")
    for ch in sorted(char_data.keys()):
        adv, polys = char_data[ch]
        compact = ",".join(
            "[" + ",".join(f"({x},{y})" for (x, y) in poly) + "]"
            for poly in polys
        )
        key = repr(ch)
        print(f"    {key}: ({adv}, [{compact}]),")
    print("}")


if __name__ == "__main__":
    main()
