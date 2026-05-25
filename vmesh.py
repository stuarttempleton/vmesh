#!/usr/bin/env python3
"""
Voltur's Meshtastic Interface

Connects to a Meshtastic device (serial or TCP), displays incoming messages
in a terminal UI, and supports optional feature plugins.

Usage:
    python vmesh.py --port /dev/ttyUSB0
    python vmesh.py --host 192.168.1.100
    python vmesh.py --port /dev/ttyUSB0 --feature features/llm.py
    python vmesh.py --port /dev/ttyUSB0 --feature features/llm.py --feature features/logger.py
"""

import argparse
from dataclasses import dataclass
import importlib.util
from platform import node
import sys
from datetime import datetime
from typing import Callable

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from textual.app import App, ComposeResult
from textual.widgets import Label, RichLog, Input, Header, Footer
from textual.suggester import Suggester
from textual.containers import Horizontal

from mesh_utils import (
    BROADCAST_ADDR,
    format_node_banner,
    format_node_info,
    format_nodes,
    get_node_display,
    parse_sendto,
    resolve_destination,
    set_timezone,
    truncate_for_mesh,
)


import re

def _strip_markup(text: str) -> str:
    return re.sub(r'\[/?[^\]]+\]', '', text).strip()

class ContextLog:
    def __init__(self, max_entries: int = 50):
        self._entries: list[str] = []
        self.max_entries = max_entries

    def append(self, text: str) -> None:
        self._entries.append(text)
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)

    def snapshot(self) -> list[str]:
        return list(self._entries)


@dataclass
class PendingAck:
    packet_id: int
    destination: str
    message: str
    timestamp: datetime
    status: str = "pending"  # "ack" | "nack" | "pending"
 
# -- Event bus ---------------------------------------------------------------

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

# -- Feature loader ----------------------------------------------------------

def load_feature(path: str, ui_write: Callable, iface, bus: MeshEventBus):
    """
    Dynamically load a feature from a .py file path.

    The file must define exactly one MeshFeature subclass.
    Returns the instantiated feature, or None on failure.
    """
    from feature_base import MeshFeature

    spec   = importlib.util.spec_from_file_location("_vmesh_feature", path)
    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"[feature] Failed to load {path}: {e}", file=sys.stderr)
        return None

    # Find the first MeshFeature subclass defined in the module
    feature_class = None
    for name in dir(module):
        obj = getattr(module, name)
        try:
            if isinstance(obj, type) and issubclass(obj, MeshFeature) and obj is not MeshFeature:
                feature_class = obj
                break
        except TypeError:
            continue

    if feature_class is None:
        print(f"[feature] No MeshFeature subclass found in {path}", file=sys.stderr)
        return None

    try:
        instance = feature_class(ui_write=ui_write, iface=iface, bus=bus)
        print(f"[feature] Loaded {feature_class.__name__} from {path}")
        return instance
    except Exception as e:
        print(f"[feature] Failed to instantiate {feature_class.__name__}: {e}", file=sys.stderr)
        return None



class NodeSuggester(Suggester):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/sendto "):
            return None
        partial = value[len("/sendto "):].lstrip('"').lower()
        for node_id, node in (self.iface.nodes or {}).items():
            user = node.get("user", {})
            candidates = [
                user.get("longName", ""),
                user.get("shortName", ""),
                node_id,  # e.g. !cafebabe
            ]
            for name in candidates:
                if name.lower().startswith(partial):
                    dest = f'"{name}"' if " " in name else name
                    return f"/sendto {dest} "
        return None

# -- App ---------------------------------------------------------------------

CORE_HELP = [
    "Commands:",
    "  /send MSG               - send a message to the mesh",
    "  /sendto NODE_ID MSG     - send a message to a specific node",
    "  /nodes                  - show nodes in the mesh",
    "  /info                   - show info about this node",
    "  /quit                   - exit the app",
    "",
    "  Tip: to copy text, use Shift+drag in a standard terminal",
]


class MeshChatApp(App):
    CSS = """
    RichLog {
        height: 1fr;
        border: solid gray;
    }

    Input {
        height: 5;
        border: solid gray;
    }

    #footer_bar {
        height: 1;
        dock: bottom;
        background: $panel;
    }

    #status {
        width: 1fr;
        padding: 0 1;
        color: $text-muted;
    }

    #char_counter {
        width: auto;
        padding: 0 1;
        text-align: right;
    }
    """

    def __init__(self, port: str = None, host: str = None, feature_paths: list[str] = None):
        super().__init__()
        self.title = "Voltur's Meshtastic Interface"

        self.bus      = MeshEventBus()
        self.features: list = []
        self._commands: dict[str, Callable] = {}
        self._node_cache: dict[str, dict] = {}
        self._ack_log: dict[int, PendingAck] = {}

        self.iface = self._connect(port=port, host=host)

        # Inject worker helpers into the bus now that we have self
        self.bus._run_worker       = lambda fn, **kw: self.run_worker(fn, thread=True, exclusive=False, **kw)
        self.bus._call_from_thread = self.call_from_thread

        # Load features after iface exists but ui_write isn't ready yet —
        # features that call ui_write at init time will queue via call_from_thread.
        for path in (feature_paths or []):
            feature = load_feature(path, self.ui_write, self.iface, self.bus)
            if feature:
                self.features.append(feature)
                self._commands.update(feature.commands())

    # -- Setup ---------------------------------------------------------------

    def _connect(self, port: str = None, host: str = None):
        pub.subscribe(self._on_receive,    "meshtastic.receive")
        pub.subscribe(self._on_connection, "meshtastic.connection.established")

        if host:
            return meshtastic.tcp_interface.TCPInterface(hostname=host)
        return meshtastic.serial_interface.SerialInterface(devPath=port)

    # -- Textual lifecycle ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="messages", wrap=True, markup=True)
        yield Input(
            placeholder="Type /help for commands", 
            max_length=180,
            suggester=NodeSuggester(self.iface),)
        
        yield Horizontal(
            Label("", id="status"),
            Label("[dim][-][/dim]", id="ack_indicator"),
            Label("0/180", id="char_counter"),
            id="footer_bar"
        )

    def on_mount(self) -> None:
        self.messages = self.query_one("#messages", RichLog)
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        val = event.value
        try:
            _, msg = parse_sendto(val)
            count = len(msg)
        except ValueError:
            count = len(val) - len("/send ") if val.startswith("/send ") else 0
        limit = 180
        color = "red" if count > 150 else "yellow" if count > 120 else "gray"
        self.query_one("#char_counter", Label).update(f"[{color}]{count}/{limit}[/{color}]")

    # -- UI helpers ----------------------------------------------------------

    def ui_write(self, text: str, log: bool = False) -> None:
        self.messages.write(text)
        if log:
            self.bus.context_log.append(_strip_markup(text))

    def ui_system_write(self, text: str, log: bool = False) -> None:
        self.ui_write(f"[dim]{text}[/dim]", log=log)

    def _format_timestamp(self, packet: dict) -> str:
        ts = packet.get("rxTime")
        if ts:
            return datetime.fromtimestamp(ts).strftime("[%H:%M]")
        return datetime.now().strftime("[%H:%M]")

    def _build_help(self) -> str:
        lines = list(CORE_HELP)
        for feature in self.features:
            extra = feature.help_text()
            if extra:
                lines.append("")
                lines.extend(extra)
        return "\n".join(lines)

    def ui_update_status(self, interface=None) -> None:
        iface = interface or self.iface
        if iface is None:
            return
        self.query_one("#status", Label).update(format_node_banner(iface))
    
    # -- Pubsub callbacks (called from Meshtastic background thread) ---------

    def _on_connection(self, interface, topic=pub.AUTO_TOPIC) -> None:
        self.call_from_thread(self.ui_write, "[bold green][CONNECTED][/bold green] Device is ready.", log=True)
        set_timezone(interface, log_fn=lambda msg: self.call_from_thread(self.ui_system_write, msg, log=True))
        self.call_from_thread(self.ui_system_write, format_node_info(interface), log=True)
        self.call_from_thread(self.ui_system_write, "\nType /help to show commands", log=True)
        self.call_from_thread(self.bus.fire, "on_connect", interface)
        self.call_from_thread(self.ui_update_status, interface)

    def _on_receive(self, packet, interface) -> None:

        portnum = packet.get("decoded", {}).get("portnum", "")
    
        if packet.get("decoded", {}).get("text"):
            self.call_from_thread(self._handle_incoming, packet)

        elif portnum == "NODEINFO_APP":
            self.call_from_thread(self._handle_node_updated, 
                                  packet["fromId"], 
                                  packet.get("decoded", {}).get("user", {}))
            self.bus.fire("on_nodeinfo", packet)

        elif portnum == "TELEMETRY_APP":
            self.call_from_thread(self.ui_update_status)
            self.bus.fire("on_telemetry", packet)

        elif portnum == "POSITION_APP":
            self.bus.fire("on_position", packet)
        
        elif portnum == "ROUTING_APP":
            request_id = packet.get("decoded", {}).get("requestId")
            error = packet.get("decoded", {}).get("routing", {}).get("errorReason", "NONE")
            if request_id:
                self.call_from_thread(self._handle_ack, request_id, error == "NONE")
            self.bus.fire("on_routing", packet)

    # -- Packet handling -----------------------------------------------------

    def _handle_incoming(self, packet: dict) -> None:
        sender = packet.get("fromId", "unknown")
        msg    = packet.get("decoded", {}).get("text", "")
        ts     = self._format_timestamp(packet)
        label  = get_node_display(self.iface, sender)

        self.ui_write(f"[dim]{ts}[/dim] [bold magenta]{label}:[/] {msg}", log=True)
        self.bus.fire("on_packet", packet)


    def _handle_node_updated(self, node_id: str, user: dict) -> None:
        prev = self._node_cache.get(node_id)

        if prev is None:
            name = user.get("longName") or user.get("shortName") or str(node_id)
            self.ui_system_write(f"~ {name} is on the mesh", log=True)
        elif user.get("longName") != prev.get("longName"):
            old = prev.get("longName") or "unknown"
            new = user.get("longName") or "unknown"
            self.ui_system_write(f"~ {old} is now known as {new}", log=True)

        self._node_cache[node_id] = dict(user)

    def _handle_ack(self, request_id: int, success: bool) -> None:
        if request_id not in self._ack_log:
            return
        self._ack_log[request_id].status = "ack" if success else "nack"
        self._update_ack_indicator()

    def _update_ack_indicator(self) -> None:
        has_nack = any(a.status == "nack" for a in self._ack_log.values())
        has_pending = any(a.status == "pending" for a in self._ack_log.values())
        label = "[red][!][/red]" if has_nack else "[dim][-][/dim]" if has_pending else "[dim green][✓][/dim green]"
        self.query_one("#ack_indicator", Label).update(label)

    # -- Input handling ------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""

        if not text:
            return

        if text.startswith("/send "):
            self._cmd_send(text[len("/send "):])

        elif text.startswith("/sendto "):
            self._cmd_sendto(text)

        elif text in ("/nodes", "/nodelist"):
            self.ui_system_write(format_nodes(self.iface), log=True)

        elif text in ("/info", "/nodeinfo", "/nodeInfo"):
            self.ui_system_write(format_node_info(self.iface), log=True)

        elif text in ("/help", "/h"):
            self.ui_system_write(self._build_help(), log=True)

        elif text in ("/q", "/quit", "/exit"):
            self.exit()

        elif text.startswith("/"):
            # Dispatch to feature commands
            cmd, _, args = text[1:].partition(" ")
            if cmd in self._commands:
                self._commands[cmd](args)
            else:
                self.ui_write(f"[red]Unknown command:[/] /{cmd}")
        
        else:
            self.ui_system_write(f"Unknown command: {text}", log=True)
            self.ui_system_write(self._build_help(), log=True)

    def _cmd_send(self, msg: str) -> None:
        msg = truncate_for_mesh(msg)
        self.iface.sendText(msg, destinationId=BROADCAST_ADDR, wantAck=False)
        self.ui_write(f"[bold cyan]You:[/] {msg}", log=True)
        self.bus.fire("on_send", BROADCAST_ADDR, msg)

    def _cmd_sendto(self, text: str) -> None:
        try:
            raw_dest, msg = parse_sendto(text)
            msg  = truncate_for_mesh(msg)
            dest = resolve_destination(raw_dest, self.iface)

            sent_packet = self.iface.sendText(msg, destinationId=dest, wantAck=True)
            packet_id = sent_packet.id

            self._ack_log[packet_id] = PendingAck(
                packet_id=packet_id,
                destination=raw_dest,
                message=msg,
                timestamp=datetime.now()
            )

            self.ui_write(
                f"[bold green][SENT -> {raw_dest} id={packet_id}][/bold green] {msg}", log=True
            )
            self.bus.fire("on_send", dest, msg)

        except ValueError as e:
            self.ui_write(f"[red]{e}[/red]")
            self.ui_system_write('Usage: /sendto "<node name or id>" <message>')

        except Exception as e:
            self.ui_write(f"[red]Parse error:[/] {e}")


# -- Entry point -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Voltur's Meshtastic Interface")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--port",    help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    group.add_argument("--host",    help="TCP hostname/IP for network-connected device")
    parser.add_argument(
        "--feature",
        action="append",
        default=[],
        metavar="PATH",
        help="Path to a feature plugin file (can be repeated)",
    )
    args = parser.parse_args()

    app = MeshChatApp(
        port=args.port,
        host=args.host,
        feature_paths=args.feature,
    )

    try:
        app.run()
    finally:
        app.iface.close()
        for feature in app.features:
            feature.shutdown()


if __name__ == "__main__":
    main()
