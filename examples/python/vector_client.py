"""Async helper for driving the vector display over WebSocket.

Usage:

    from vector_client import VectorDisplay, Frame

    async def main():
        async with VectorDisplay() as d:
            f = Frame()
            f.move_to(0, 0)
            f.draw_to(0.5, 0.5)
            await d.send(f)

            evt = d.event_nowait()  # drain back-channel without blocking
            ...
"""

import asyncio
import json
import struct
from typing import Optional

import websockets


class Frame:
    """Accumulates beam commands. One Frame == one WS message."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def move_to(self, x: float, y: float) -> None:
        self._buf += struct.pack("<Bff", 0, x, y)

    def draw_to(self, x: float, y: float, intensity: float = 1.0) -> None:
        self._buf += struct.pack("<Bfff", 1, x, y, intensity)

    def line(self, x0: float, y0: float, x1: float, y1: float, intensity: float = 1.0) -> None:
        self.move_to(x0, y0)
        self.draw_to(x1, y1, intensity)

    def polyline(self, points, intensity: float = 1.0) -> None:
        it = iter(points)
        try:
            x, y = next(it)
        except StopIteration:
            return
        self.move_to(x, y)
        for x, y in it:
            self.draw_to(x, y, intensity)

    def dot(self, x: float, y: float, intensity: float = 1.0) -> None:
        # Short non-zero horizontal segment. A literally-zero segment evaluates
        # to brightness 0 because the integrated-gaussian-along-segment factor
        # is 0; a tiny non-zero segment renders as a bright spot via the
        # inverse-segment-length brightness boost. ε of ~0.0008 NDC ≈ 1 pixel
        # at typical window sizes.
        eps = 0.0008
        self.move_to(x - eps, y)
        self.draw_to(x + eps, y, intensity)

    def to_bytes(self) -> bytes:
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


class VectorDisplay:
    """Async context manager around a WS connection to the display."""

    def __init__(self, url: str = "ws://localhost:5002"):
        self.url = url
        self.ws = None
        self.viewport = (1024, 768)  # populated by `hello` on connect
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task = None

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url)
        self._reader_task = asyncio.create_task(self._reader_loop())
        # Block for the initial Hello so callers can immediately read .viewport.
        hello = await self._event_queue.get()
        if isinstance(hello, dict) and hello.get("type") == "hello":
            self.viewport = (hello["width"], hello["height"])
        else:
            # Out-of-order? Put it back; .viewport stays at default.
            await self._event_queue.put(hello)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws:
            await self.ws.close()

    async def _reader_loop(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, (bytes, bytearray)):
                    continue  # server doesn't send us binary, ignore if it ever does
                try:
                    event = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "resize":
                    self.viewport = (event["width"], event["height"])
                await self._event_queue.put(event)
        except Exception:
            # Connection closed or other; reader is done.
            pass

    async def send(self, frame: Frame) -> None:
        """Send one frame. Raises if the connection is gone."""
        await self.ws.send(frame.to_bytes())

    async def next_event(self, timeout: Optional[float] = None):
        """Block until the next event arrives (or timeout). Returns dict or None."""
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def event_nowait(self):
        """Pop one pending event without blocking. Returns dict or None."""
        try:
            return self._event_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def drain_events(self):
        """Yield all pending events without blocking. Iterator of dicts."""
        while True:
            event = self.event_nowait()
            if event is None:
                return
            yield event

    @property
    def aspect(self) -> float:
        w, h = self.viewport
        return w / h if h else 1.0
