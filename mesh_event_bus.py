"""
mesh_event_bus.py — Meshtastic event bus
"""

from typing import Callable
from context_log import ContextLog
import sys


class MeshEventBus:
    """
    Lightweight event bus shared between the app and all features.

    Features subscribe to named events; the app fires them.
    The bus also exposes run_worker and call_from_thread so features
    never need to import from vmesh or Textual directly.

    Available events:
        on_packet(packet: dict)          — incoming text packet (Textual thread)
        on_connect(interface)            — device ready (Textual thread)
        on_send(destination, message)    — user sent a message (Textual thread)
    """

    EVENTS = ("on_packet", "on_connect", "on_send")

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {e: [] for e in self.EVENTS}
        self._run_worker    = None   # injected by the app after construction
        self._call_from_thread = None
        self.context_log = ContextLog()

    def on(self, event: str, handler: Callable) -> None:
        if event not in self._handlers:
            raise ValueError(f"Unknown event '{event}'. Valid events: {self.EVENTS}")
        self._handlers[event].append(handler)

    def fire(self, event: str, *args, **kwargs) -> None:
        for handler in self._handlers.get(event, []):
            try:
                handler(*args, **kwargs)
            except Exception as e:
                # Don't let a broken feature take down the whole app
                print(f"[feature error] {event} handler {handler}: {e}", file=sys.stderr)

    def run_worker(self, fn: Callable, **kwargs) -> None:
        """Run a callable in a background thread (via the Textual app worker)."""
        if self._run_worker:
            self._run_worker(fn, **kwargs)

    def call_from_thread(self, fn: Callable, *args, **kwargs) -> None:
        """Schedule a UI call from a background thread."""
        if self._call_from_thread:
            self._call_from_thread(fn, *args, **kwargs)