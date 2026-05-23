# Wire Protocol

The display speaks two transports in parallel. Both carry the same beam
commands; only WebSocket carries the back-channel.

| Transport | Default port | Direction         | Best for                                  |
|-----------|--------------|-------------------|-------------------------------------------|
| TCP       | 5001         | Client → Display  | Push-only generators, native, lowest cost |
| WebSocket | 5002         | Both directions   | Interactive clients, browsers, WASM       |

Both transports use a shared coordinate system and beam-command payload
format. Most users want **WebSocket** unless they're driving the display
from a CPU-bound process where every microsecond matters.

The display starts both listeners by default. Disable either with
`--no-tcp-port` / `--no-ws-port`; override ports with `--tcp-port N` /
`--ws-port N`.

---

## 1. Coordinates

The beam lives in normalized device coordinates (NDC):

- `x` ∈ [-1, 1], left → right
- `y` ∈ [-1, 1], bottom → top
- Drawing outside this range is allowed but invisible

The display does **not** correct for window aspect ratio. A unit circle
in NDC will look like an ellipse in a non-square window. Clients that
care should multiply x by `height / width` (or vice versa) using the
viewport size from the `hello` / `resize` events (WebSocket only) — or
just keep the window square.

A `MoveTo` blanks the beam (no line drawn) but the beam still takes time
to traverse the distance — that time contributes to intra-frame phosphor
decay, so long blanked moves still affect how fresh the surrounding
vectors look.

A `DrawTo` draws a line from the current beam position to the target,
with the given intensity in [0, 1]. Intensities above 1 are accepted
(the texture is HDR float) and useful for emphasizing bright vectors,
but the phosphor saturation model will absorb less of the excess as
brightness piles up.

The implicit beam start position at the beginning of each frame is the
position the beam ended on at the end of the previous frame. There is
no automatic "home" — start each frame with a `MoveTo` if you care
about deterministic timing.

---

## 2. Beam commands (payload format)

The payload is a packed sequence of commands. Each command begins with
a single tag byte. All multi-byte fields are little-endian.

```
MoveTo:  [0u8][f32 x][f32 y]                       9 bytes
DrawTo:  [1u8][f32 x][f32 y][f32 intensity]       13 bytes
```

A single payload may contain any number of commands. A typical refresh
batch is a few hundred to a few thousand. The renderer caps active line
segments at 8192 per frame — anything beyond is silently dropped.

---

## 3. TCP transport

Each "frame" is a length-prefixed payload:

```
[u32 LE payload_byte_length][payload bytes]
```

The display reads payloads as fast as the client sends them and keeps
only the most recent one between display frames (older payloads dropped
in the channel). Client typically sends one payload per render frame
(60 Hz). Maximum payload size is 1 MB.

When the client disconnects, the display automatically falls back to
the built-in Lissajous demo until something new connects.

### Python example (TCP)

```python
import socket, struct

s = socket.create_connection(('localhost', 5001))
payload  = struct.pack('<Bff', 0, 0.0, 0.0)         # MoveTo(0, 0)
payload += struct.pack('<Bfff', 1, 0.5, 0.5, 1.0)   # DrawTo(0.5, 0.5)
s.sendall(struct.pack('<I', len(payload)) + payload)
```

---

## 4. WebSocket transport

WebSocket framing already provides message boundaries, so the length
prefix is **not** used. Each binary WS message body = one full payload
of commands.

Text frames in either direction are reserved (currently logged and
discarded for client→server; used for events server→client, see below).

### Client → Display (binary, beam commands)

Same payload layout as TCP, no `u32` prefix:

```
[BeamCommand payload bytes]
```

### Display → Client (text, JSON events)

Each text frame is a single-line JSON object with a `type` field that
selects the schema:

| `type`         | Schema                                                                    | Sent when                              |
|----------------|---------------------------------------------------------------------------|----------------------------------------|
| `hello`        | `{"type":"hello","width":W,"height":H}`                                   | Once, immediately on connect           |
| `resize`       | `{"type":"resize","width":W,"height":H}`                                  | Window resized                          |
| `cursor_move`  | `{"type":"cursor_move","x":X,"y":Y}`                                      | Cursor moves over the display window    |
| `mouse_button` | `{"type":"mouse_button","x":X,"y":Y,"button":B,"pressed":P}`              | Mouse button pressed or released        |
| `key`          | `{"type":"key","key":K,"pressed":P}`                                      | Keyboard key pressed or released        |

Field semantics:

- `width`, `height` — current display viewport in physical pixels
- `x`, `y` — cursor position in NDC (same coordinate system as beam commands)
- `button` — `"left"` / `"right"` / `"middle"`; other buttons are not reported
- `key` — for printable keys, the character itself (`"a"`, `"1"`).
  For special keys, one of: `"Space"`, `"Enter"`, `"Escape"`, `"Tab"`,
  `"Backspace"`, `"Delete"`, `"ArrowUp"`, `"ArrowDown"`, `"ArrowLeft"`,
  `"ArrowRight"`, `"Shift"`, `"Control"`, `"Alt"`, `"Home"`, `"End"`,
  `"PageUp"`, `"PageDown"`. Unmapped keys arrive as `"Unidentified"`.
- `pressed` — `true` on press, `false` on release. Key repeat is **not**
  filtered out — the OS may send many `pressed:true` events for a held key.

The display sends events as the user generates them; there is no batching.
Expect bursts during cursor movement.

The display continues to handle a couple of keys locally regardless of
the connected client (`Esc` quits, `w/a/s/d/q/e/r/f` tune render
parameters). Those keys are still forwarded to clients.

### Python example (WebSocket, with events)

```python
import asyncio, json, struct, websockets

async def main():
    async with websockets.connect("ws://localhost:5002") as ws:
        # Receive initial hello
        hello = json.loads(await ws.recv())
        print("viewport:", hello)

        # Send one frame: line from (0,0) to (0.5, 0.5)
        payload  = struct.pack('<Bff', 0, 0.0, 0.0)
        payload += struct.pack('<Bfff', 1, 0.5, 0.5, 1.0)
        await ws.send(payload)

        # Listen for input events
        async for msg in ws:
            event = json.loads(msg)
            print(event)

asyncio.run(main())
```

---

## 5. Connection lifecycle

- The display accepts multiple simultaneous WS clients (and one TCP
  client at a time). Whichever transport sends commands most recently
  "wins" for the next display frame.
- A WS client that goes idle keeps receiving events. A TCP client that
  goes idle keeps the connection open but contributes no commands until
  it sends again.
- Disconnect causes the display to fall back to the built-in Lissajous
  demo on the next idle frame.

---

## 6. Versioning

The wire format is currently v1 — no version field, no negotiation.
If/when v2 lands, it will use a different default port and clients can
probe both. Don't ship long-lived integrations against this protocol
without pinning a commit.
