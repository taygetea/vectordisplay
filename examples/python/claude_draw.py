"""Claude draws on the vector display via SVG.

Modes:

    python claude_draw.py "a sigil"
        Generate one drawing for the prompt, hold it on the display.
        Ctrl-C to quit. New prompts can be typed at the terminal.

    python claude_draw.py
        Same, but starts with no drawing. Type prompts at the prompt.

Claude is constrained to emit a narrow SVG subset that maps cleanly to
the vector display (stroke-only, no fills, no text, no gradients).
"""

import asyncio
import os
import re
import subprocess
import sys
import time

from anthropic import AsyncAnthropic

from svg import svg_to_polylines
from vector_client import Frame, VectorDisplay


MODEL = "claude-sonnet-4-5"

SYSTEM_PROMPT = """You are an artist working on a vintage vector CRT display \
(HP 1345A-style). Your medium is SVG, but with strict constraints so the \
drawing renders correctly on the hardware:

CONSTRAINTS (the display physically cannot show these):
- No fills. Every element MUST have fill="none". The display only draws strokes.
- Single color. The display is monochrome (P31 phosphor green). Do not use \
multiple colors — just stroke="currentColor" or stroke="black", same effect.
- No text. Font rendering isn't supported. If you need letters, draw them as \
line strokes inside <path>.
- No gradients, no patterns, no filters, no clip paths, no masks.

ALLOWED ELEMENTS:
<line>, <polyline>, <polygon>, <rect>, <circle>, <ellipse>, <path>, <g>

PATH COMMANDS: M, L, H, V, C, S, Q, T, A, Z (uppercase or lowercase).

STYLE GUIDANCE:
- Aim for visually interesting line-art compositions. Think technical \
illustration, constellation maps, sigils, sailing ships, plant diagrams, \
mechanical drawings, x-ray hands, hex maps, mandalas — anything that reads \
clearly as line drawing.
- Be generous with detail. A drawing of 50-200 strokes looks rich on this \
medium; the persistence trails and beam glow fill in atmosphere.
- Use viewBox="0 0 200 200" (or 0 0 1000 1000) so coordinates are clean.
- stroke-width is ignored by the display (the beam width is a global \
hardware property), so don't worry about it.

OUTPUT:
Return ONLY the <svg>...</svg> element. No explanation, no markdown fences, \
no commentary. Just the SVG."""


def get_api_key() -> str:
    if k := os.environ.get("ANTHROPIC_API_KEY"):
        return k
    # Fall back to simonw/llm key store in WSL.
    try:
        result = subprocess.run(
            ["wsl.exe", "llm", "keys", "get", "claude"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception as e:
        raise SystemExit(
            f"No ANTHROPIC_API_KEY in env and could not fetch from wsl llm: {e}"
        )


_SVG_RE = re.compile(r"<svg[\s\S]*?</svg>", re.IGNORECASE)


def extract_svg(text: str) -> str:
    m = _SVG_RE.search(text)
    if not m:
        raise ValueError(f"no <svg> in response:\n{text[:400]}")
    return m.group()


async def generate(client: AsyncAnthropic, prompt: str) -> str:
    """Ask Claude for one drawing. Returns the SVG text."""
    t0 = time.monotonic()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Draw: {prompt}"}],
    )
    dt = time.monotonic() - t0
    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    print(f"  [{dt:.1f}s, {in_tok}→{out_tok} tok]")
    return extract_svg(raw)


def build_frame(svg_text: str, intensity: float = 1.0) -> Frame:
    f = Frame()
    for poly in svg_to_polylines(svg_text):
        if len(poly) >= 2:
            f.polyline(poly, intensity)
    return f


class State:
    """Mutable shared between the render loop and the prompt loop."""
    def __init__(self):
        self.current_frame: Frame = Frame()
        self.current_label: str = ""
        self.generating: bool = False


async def render_loop(display: VectorDisplay, state: State):
    """Push the current frame to the display 60 times a second."""
    while True:
        try:
            await display.send(state.current_frame)
        except Exception as e:
            print(f"\n[render] send failed: {e}")
            return
        # Drain events; ignore for now (could echo cursor, etc.)
        for _ in display.drain_events():
            pass
        await asyncio.sleep(1 / 60)


async def prompt_loop(client: AsyncAnthropic, state: State):
    """Read prompts from stdin and update the current frame."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            prompt = await loop.run_in_executor(None, lambda: input("> ").strip())
        except EOFError:
            return  # stdin closed; tell main to wind down
        if not prompt:
            continue
        if prompt in (":q", ":quit", "exit"):
            return
        if state.generating:
            print("(busy, ignoring — try again in a moment)")
            continue
        state.generating = True
        try:
            svg = await generate(client, prompt)
            state.current_frame = build_frame(svg)
            state.current_label = prompt
            print(f"  -> {len(state.current_frame)} bytes of beam commands")
        except Exception as e:
            print(f"  !! {e}")
        finally:
            state.generating = False


async def main():
    key = get_api_key()
    client = AsyncAnthropic(api_key=key)
    state = State()

    # If a prompt was passed on the command line, seed with it before opening
    # the interactive loop.
    initial_prompt = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""

    async with VectorDisplay() as d:
        print(f"connected; viewport {d.viewport}")
        print(f"model {MODEL}; type a prompt and press enter, or :q to quit")

        if initial_prompt:
            print(f"\n> {initial_prompt}")
            try:
                svg = await generate(client, initial_prompt)
                state.current_frame = build_frame(svg)
                state.current_label = initial_prompt
                print(f"  -> {len(state.current_frame)} bytes of beam commands")
            except Exception as e:
                print(f"  !! {e}")

        renderer = asyncio.create_task(render_loop(d, state))
        prompter = asyncio.create_task(prompt_loop(client, state))
        done, pending = await asyncio.wait(
            [renderer, prompter], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
