# SVG examples for the vector display

The display is, secretly, an SVG viewer. `examples/python/svg.py` parses a
narrow subset of SVG into beam commands; `examples/python/svg_view.py`
streams a static file to the display at 60 Hz so the phosphor stays lit.

```
python examples/python/svg_view.py examples/svg/compass_rose.svg
python examples/python/svg_view.py examples/svg/orion.svg
```

## Why this works

Claude (and most LLMs) are extensively trained on SVG. When you constrain
output to single-stroke, single-color, no-text, no-fills, no-gradients —
that's the slice of SVG that's almost always correctly emitted, and it
happens to be exactly what a vector CRT can physically draw.

`examples/python/claude_draw.py` wraps the Anthropic API with a system
prompt that locks Claude into that subset, then pipes generations to the
display:

```
python examples/python/claude_draw.py "a vintage astrolabe"
```

(Requires an Anthropic API key in `ANTHROPIC_API_KEY` or accessible via
`wsl.exe llm keys get claude`.)

## Supported SVG subset

| Element       | Notes                                                      |
|---------------|------------------------------------------------------------|
| `<line>`      | Direct                                                     |
| `<polyline>`  | Direct                                                     |
| `<polygon>`   | Closes back to start automatically                         |
| `<rect>`      | Outline only (rounded corners ignored)                     |
| `<circle>`    | Sampled to a 32-gon                                        |
| `<ellipse>`   | Sampled to a 32-gon                                        |
| `<path>`      | M, L, H, V, C, S, Q, T, A, Z. Curves flattened recursively |
| `<g>`         | Recursed into; transforms not yet applied                  |

Everything else (text, gradients, filters, masks, etc.) is silently
ignored. Fills are always dropped — every shape becomes its outline.

## Coordinate handling

`viewBox` determines source bounds. If absent, the renderer computes
bounds from the actual stroke geometry. Either way the result is fit to
NDC preserving aspect, centered, Y flipped (SVG y-down → beam y-up).

## Writing your own

A minimal example:

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <circle cx="100" cy="100" r="50"/>
  <line x1="0" y1="0" x2="200" y2="200"/>
</svg>
```

Save, point `svg_view.py` at it, and the display will hold the drawing.
