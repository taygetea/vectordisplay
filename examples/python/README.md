# Python client examples

These clients drive the display over WebSocket. Start the display first
(`cargo run --release` from the repo root), then run an example:

```
pip install websockets
python hello.py        # rotating square
python spiral.py       # animated spiral with intensity variation
python wargames.py     # US map, click-to-launch missile trajectories
```

`Esc` or closing the display window will exit; the example client will
notice the dropped connection on the next send and quit. Multiple example
clients can connect at once over WebSocket — the most-recent payload wins
each render frame.

## Files

| File              | What                                                                       |
|-------------------|----------------------------------------------------------------------------|
| `vector_client.py`| Shared helper: `Frame` builder, `VectorDisplay` async context manager.     |
| `hershey.py`      | Subset of the Hershey "futural" vector font for text labels.               |
| `hello.py`        | Rotating square. Smallest possible interactive client.                     |
| `spiral.py`       | Animated spiral. Shows what continuous-beam content looks like.            |
| `wargames.py`     | US map demo. Click anywhere to launch a missile from the nearest silo;     |
|                   | space toggles a denser "attack mode". Reads cursor + click events from     |
|                   | the back-channel.                                                          |
| `data/usa.json`   | Pre-baked Natural Earth state outlines + city positions for wargames.      |
| `bake_data.py`    | One-time script that re-generates `data/usa.json` from Natural Earth.      |
| `bake_font.py`    | One-time script that re-generates the Hershey font subset in `hershey.py`. |

You don't need to run the bake scripts unless you want different
simplification, different cities, or a different font.

## Aspect ratio

NDC is square (-1 to 1 in both axes), but the display window may not be.
The `hello` and `resize` events report the current viewport in pixels;
`VectorDisplay.aspect` exposes the width/height ratio. `hello.py` shows
the simple correction: shrink the longer axis so a unit circle looks
round in any window. `wargames.py` doesn't bother — the US map looks
fine somewhat squished, and Albers is forgiving.
