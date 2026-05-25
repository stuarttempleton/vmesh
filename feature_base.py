"""
feature_base.py — Base class for vmesh features / plugins.

A feature is a self-contained module that can:
  - Register slash commands  (/mycommand <args>)
    - Subscribe to app events  (on_packet, on_connect, on_send, on_nodeinfo, on_telemetry, on_position, on_routing)
  - Write output to the UI   (via the ui_write callable)
  - Send mesh messages       (via the iface reference)

To create a feature:
  1. Subclass MeshFeature
  2. Override commands() and/or subscribe to events in __init__
  3. Drop the file in the features/ directory
  4. Pass it via --feature on the command line

Example:
    class MyFeature(MeshFeature):
        def commands(self):
            return {"hello": self._cmd_hello}

        def _cmd_hello(self, args: str):
            self.ui_write("Hello from MyFeature!")
"""

from __future__ import annotations
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from vmesh import MeshEventBus


class MeshFeature:
    """
    Base class for all vmesh features.

    Subclasses receive ui_write and iface at construction time so they never
    need to import from vmesh.py directly.

    Events are subscribed via the bus:
        bus.on("on_packet", self.handle_packet)

    Available events:
        on_packet(packet: dict)          — incoming text packet (Textual thread)
        on_connect(interface)            — device ready (Textual thread)
        on_send(destination, message)    — user sent a message (Textual thread)
        on_nodeinfo(packet: dict)        — node info update packet
        on_telemetry(packet: dict)       — telemetry packet
        on_position(packet: dict)        — position packet
        on_routing(packet: dict)         — routing/ack packet
    """

    def __init__(self, ui_write: Callable, iface, bus: "MeshEventBus"):
        self.ui_write = ui_write
        self.iface    = iface
        self.bus      = bus

    def commands(self) -> dict[str, Callable[[str], None]]:
        """
        Return a dict mapping command names to handler callables.

        Keys are the bare command word (no slash), e.g. "llm", "autoreply".
        Handlers receive everything after the command word as a single string.

        Example:
            {"llm": self._cmd_llm, "autoreply": self._cmd_autoreply}
        """
        return {}

    def completions(self) -> dict[str, str]:
        """
        Return optional completion metadata per command.

        Keys are bare command words (no slash). Values are completion kinds.
        Supported kinds:
            "node_target"  - first argument should autocomplete to node name/id

        Example:
            {"trout": "node_target"}
        """
        return {}

    def help_text(self) -> list[str]:
        """
        Return a list of help lines to include in /help output.
        Each line should follow the existing format:
            "  /cmd ARGS     - description"
        """
        return []

    def shutdown(self) -> None:
        """Called on app exit. Override to flush/save state."""
        pass
