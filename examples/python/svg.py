"""Minimal SVG → beam-command renderer.

Treats an SVG document as a set of strokes. Fills are ignored (every
shape becomes its outline). Color is ignored (the display tints
globally). The supported subset is whatever a vector CRT could plausibly
draw and what Claude tends to emit when constrained:

  <line>, <polyline>, <polygon>, <rect>, <circle>, <ellipse>, <path>
  path commands: M m L l H h V v C c S s Q q T t A a Z z

Curves are flattened by recursive bezier subdivision; ellipses and
circles by parametric sampling; arcs via Snyder's endpoint→center
parameterization then sampled as elliptic arcs.

Usage:

    from svg import svg_to_polylines
    polylines = svg_to_polylines(svg_string, fit_to=(0.9, 0.9))
    frame = Frame()
    for poly in polylines:
        frame.polyline(poly)

`fit_to` is (half_width, half_height) in NDC. The renderer fits the
viewBox (or, falling back, the bounding box of all strokes) into that
target preserving aspect.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Iterable, List, Tuple

Point = Tuple[float, float]
Polyline = List[Point]

# How many segments to subdivide a unit-arc (radians) of a curve into.
# Higher = smoother curves at more vertex cost.
_CURVE_RES = 32  # samples per full circle for circles/ellipses
_BEZIER_TOL = 0.5  # flatness tolerance in viewBox units; subdivide until below


# --- Path data tokenizer -----------------------------------------------------

# Numbers may be: -1.5, +.5, 1e-5, .5, etc. Note leading sign or decimal.
_NUM_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_CMD_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")


def _tokenize_path(d: str):
    """Yield ('cmd', letter) or ('num', float) tokens from an SVG path d."""
    i = 0
    while i < len(d):
        c = d[i]
        if c.isspace() or c == ",":
            i += 1
            continue
        if _CMD_RE.match(c):
            yield ("cmd", c)
            i += 1
            continue
        m = _NUM_RE.match(d, i)
        if not m:
            i += 1
            continue
        yield ("num", float(m.group()))
        i = m.end()


def _take_numbers(toks, n) -> List[float]:
    out = []
    for _ in range(n):
        kind, val = next(toks)
        if kind != "num":
            raise ValueError("expected number in path data")
        out.append(val)
    return out


# --- Curve flattening --------------------------------------------------------

def _flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point, tol: float, out: Polyline):
    """Recursively subdivide a cubic bezier until each segment is nearly flat,
    appending the endpoint of each accepted segment to `out`. (Start point
    must already be in `out`.)"""
    # Flatness test: max perpendicular distance of control points from chord.
    ax, ay = p0
    bx, by = p3
    dx, dy = bx - ax, by - ay
    chord_len = math.hypot(dx, dy)
    if chord_len < 1e-9:
        # Degenerate; just emit the endpoint.
        out.append(p3)
        return
    # Distance from p1 and p2 to line (p0, p3)
    def dist(px, py):
        return abs((py - ay) * dx - (px - ax) * dy) / chord_len
    d1 = dist(*p1)
    d2 = dist(*p2)
    if max(d1, d2) <= tol:
        out.append(p3)
        return
    # de Casteljau split at t = 0.5
    m01 = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
    m12 = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    m23 = ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2)
    m012 = ((m01[0] + m12[0]) / 2, (m01[1] + m12[1]) / 2)
    m123 = ((m12[0] + m23[0]) / 2, (m12[1] + m23[1]) / 2)
    mid = ((m012[0] + m123[0]) / 2, (m012[1] + m123[1]) / 2)
    _flatten_cubic(p0, m01, m012, mid, tol, out)
    _flatten_cubic(mid, m123, m23, p3, tol, out)


def _flatten_quadratic(p0: Point, p1: Point, p2: Point, tol: float, out: Polyline):
    # Convert to cubic and reuse (standard trick).
    c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1]))
    _flatten_cubic(p0, c1, c2, p2, tol, out)


def _flatten_arc(p0: Point, rx: float, ry: float, phi_deg: float,
                 large: bool, sweep: bool, p1: Point, tol: float, out: Polyline):
    """SVG arc endpoint parameterization → sampled polyline. Standard
    formulas from the SVG implementation notes."""
    if abs(rx) < 1e-9 or abs(ry) < 1e-9:
        out.append(p1)
        return
    x1, y1 = p0
    x2, y2 = p1
    phi = math.radians(phi_deg)
    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)
    # Step 1: compute (x1', y1')
    dx = (x1 - x2) / 2
    dy = (y1 - y2) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    # Correct radii if needed
    rx = abs(rx)
    ry = abs(ry)
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    # Step 2: center
    sign = -1 if large == sweep else 1
    sq = max(
        0.0,
        (rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p)
        / (rx * rx * y1p * y1p + ry * ry * x1p * x1p),
    )
    coef = sign * math.sqrt(sq)
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2
    # Step 3: angles
    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        mag = math.hypot(ux, uy) * math.hypot(vx, vy)
        cos_a = max(-1.0, min(1.0, dot / mag if mag else 0))
        a = math.acos(cos_a)
        if ux * vy - uy * vx < 0:
            a = -a
        return a
    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    # Sample
    n = max(8, int(_CURVE_RES * abs(delta) / (2 * math.pi)))
    for i in range(1, n + 1):
        t = theta1 + delta * i / n
        x = cos_phi * rx * math.cos(t) - sin_phi * ry * math.sin(t) + cx
        y = sin_phi * rx * math.cos(t) + cos_phi * ry * math.sin(t) + cy
        out.append((x, y))


# --- Element-level handlers --------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_points(s: str) -> List[Point]:
    nums = [float(m.group()) for m in _NUM_RE.finditer(s)]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _emit_path(d: str) -> List[Polyline]:
    """Parse one <path d="..."> into a list of polylines (subpaths)."""
    polylines: List[Polyline] = []
    toks = iter(list(_tokenize_path(d)))
    cur: Point = (0.0, 0.0)
    start_subpath: Point = (0.0, 0.0)
    last_ctrl: Point = None  # for smooth (S/T) continuation
    last_cmd: str = ""
    current_poly: Polyline = []

    def flush():
        nonlocal current_poly
        if len(current_poly) >= 2:
            polylines.append(current_poly)
        current_poly = []

    def absolute(rel: bool, dx: float, dy: float) -> Point:
        return (cur[0] + dx, cur[1] + dy) if rel else (dx, dy)

    while True:
        try:
            kind, val = next(toks)
        except StopIteration:
            break
        if kind != "cmd":
            # Implicit repeat of previous command — push the number back as
            # if a new "cmd" arrived with the previous letter.
            toks = iter([(kind, val)] + list(toks))
            kind, val = "cmd", last_cmd
        cmd = val
        rel = cmd.islower()
        cu = cmd.upper()

        if cu == "M":
            nums = _take_numbers(toks, 2)
            p = absolute(rel, *nums)
            flush()
            current_poly = [p]
            cur = p
            start_subpath = p
            # Subsequent implicit-pair commands after M act as L (per spec)
            last_cmd = "l" if rel else "L"
        elif cu == "L":
            nums = _take_numbers(toks, 2)
            p = absolute(rel, *nums)
            current_poly.append(p)
            cur = p
            last_cmd = cmd
        elif cu == "H":
            nums = _take_numbers(toks, 1)
            p = (cur[0] + nums[0], cur[1]) if rel else (nums[0], cur[1])
            current_poly.append(p)
            cur = p
            last_cmd = cmd
        elif cu == "V":
            nums = _take_numbers(toks, 1)
            p = (cur[0], cur[1] + nums[0]) if rel else (cur[0], nums[0])
            current_poly.append(p)
            cur = p
            last_cmd = cmd
        elif cu == "C":
            nums = _take_numbers(toks, 6)
            c1 = absolute(rel, nums[0], nums[1])
            c2 = absolute(rel, nums[2], nums[3])
            p = absolute(rel, nums[4], nums[5])
            _flatten_cubic(cur, c1, c2, p, _BEZIER_TOL, current_poly)
            last_ctrl = c2
            cur = p
            last_cmd = cmd
        elif cu == "S":
            nums = _take_numbers(toks, 4)
            # Reflect previous control point.
            if last_cmd.upper() in ("C", "S") and last_ctrl is not None:
                c1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1])
            else:
                c1 = cur
            c2 = absolute(rel, nums[0], nums[1])
            p = absolute(rel, nums[2], nums[3])
            _flatten_cubic(cur, c1, c2, p, _BEZIER_TOL, current_poly)
            last_ctrl = c2
            cur = p
            last_cmd = cmd
        elif cu == "Q":
            nums = _take_numbers(toks, 4)
            c = absolute(rel, nums[0], nums[1])
            p = absolute(rel, nums[2], nums[3])
            _flatten_quadratic(cur, c, p, _BEZIER_TOL, current_poly)
            last_ctrl = c
            cur = p
            last_cmd = cmd
        elif cu == "T":
            nums = _take_numbers(toks, 2)
            if last_cmd.upper() in ("Q", "T") and last_ctrl is not None:
                c = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1])
            else:
                c = cur
            p = absolute(rel, nums[0], nums[1])
            _flatten_quadratic(cur, c, p, _BEZIER_TOL, current_poly)
            last_ctrl = c
            cur = p
            last_cmd = cmd
        elif cu == "A":
            nums = _take_numbers(toks, 7)
            rx, ry, phi = nums[0], nums[1], nums[2]
            large = nums[3] != 0
            sweep = nums[4] != 0
            p = absolute(rel, nums[5], nums[6])
            _flatten_arc(cur, rx, ry, phi, large, sweep, p, _BEZIER_TOL, current_poly)
            cur = p
            last_cmd = cmd
        elif cu == "Z":
            if current_poly and current_poly[0] != cur:
                current_poly.append(current_poly[0])
            cur = start_subpath
            flush()
            last_cmd = cmd
        else:
            # Unknown command, skip its expected operands. Safest: bail.
            break

    flush()
    return polylines


def _emit_circle(cx: float, cy: float, r: float, n: int = _CURVE_RES) -> Polyline:
    pts = []
    for i in range(n + 1):
        a = (i / n) * 2 * math.pi
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _emit_ellipse(cx: float, cy: float, rx: float, ry: float, n: int = _CURVE_RES) -> Polyline:
    pts = []
    for i in range(n + 1):
        a = (i / n) * 2 * math.pi
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def _emit_rect(x: float, y: float, w: float, h: float) -> Polyline:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]


def _attr_float(el, name: str, default: float = 0.0) -> float:
    v = el.attrib.get(name)
    if v is None:
        return default
    # Strip unit suffix (px, pt, %, etc.) — vector display doesn't care
    m = _NUM_RE.match(v.strip())
    return float(m.group()) if m else default


# --- Public entry point ------------------------------------------------------

def svg_to_polylines(
    svg_text: str,
    fit_to: Tuple[float, float] = (0.9, 0.9),
    pad: float = 0.02,
) -> List[Polyline]:
    """Parse the SVG and return polylines in NDC.

    fit_to: (half_width, half_height) of the target NDC box.
    pad: additional margin inside fit_to (NDC units).
    """
    root = ET.fromstring(svg_text)
    raw: List[Polyline] = []

    def visit(el):
        tag = _strip_ns(el.tag)
        if tag == "line":
            raw.append([
                (_attr_float(el, "x1"), _attr_float(el, "y1")),
                (_attr_float(el, "x2"), _attr_float(el, "y2")),
            ])
        elif tag == "polyline":
            pts = _parse_points(el.attrib.get("points", ""))
            if len(pts) >= 2:
                raw.append(pts)
        elif tag == "polygon":
            pts = _parse_points(el.attrib.get("points", ""))
            if len(pts) >= 2:
                raw.append(pts + [pts[0]])
        elif tag == "rect":
            raw.append(_emit_rect(
                _attr_float(el, "x"),
                _attr_float(el, "y"),
                _attr_float(el, "width"),
                _attr_float(el, "height"),
            ))
        elif tag == "circle":
            r = _attr_float(el, "r")
            if r > 0:
                raw.append(_emit_circle(_attr_float(el, "cx"), _attr_float(el, "cy"), r))
        elif tag == "ellipse":
            rx = _attr_float(el, "rx")
            ry = _attr_float(el, "ry")
            if rx > 0 and ry > 0:
                raw.append(_emit_ellipse(_attr_float(el, "cx"), _attr_float(el, "cy"), rx, ry))
        elif tag == "path":
            d = el.attrib.get("d")
            if d:
                raw.extend(_emit_path(d))
        # Recurse for <g> and similar grouping elements.
        for child in el:
            visit(child)

    visit(root)

    # Determine source bounds: prefer viewBox; otherwise compute from strokes.
    viewbox = root.attrib.get("viewBox")
    if viewbox:
        nums = [float(m.group()) for m in _NUM_RE.finditer(viewbox)]
        if len(nums) == 4:
            min_x, min_y, w, h = nums
            max_x = min_x + w
            max_y = min_y + h
        else:
            viewbox = None
    if not viewbox:
        if not raw:
            return []
        xs = [p[0] for poly in raw for p in poly]
        ys = [p[1] for poly in raw for p in poly]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

    span_x = max(1e-9, max_x - min_x)
    span_y = max(1e-9, max_y - min_y)

    # Fit preserving aspect.
    avail_x = 2 * (fit_to[0] - pad)
    avail_y = 2 * (fit_to[1] - pad)
    scale = min(avail_x / span_x, avail_y / span_y)
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2

    def transform(p: Point) -> Point:
        # Flip Y: SVG is y-down, NDC is y-up.
        return ((p[0] - cx) * scale, -(p[1] - cy) * scale)

    return [[transform(p) for p in poly] for poly in raw]


def emit_to_frame(svg_text: str, frame, intensity: float = 1.0, **kwargs):
    """Convenience: parse SVG and emit straight to a Frame."""
    for poly in svg_to_polylines(svg_text, **kwargs):
        if len(poly) >= 2:
            frame.polyline(poly, intensity)
