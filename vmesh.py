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
from pathlib import Path
from platform import node
import shlex
import sys
from datetime import datetime
from typing import Callable

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from textual.app import App, ComposeResult
from textual.widgets import Label, RichLog, Input, Header
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
    strip_markup,
    utf8_len,
)

from mesh_event_bus import MeshEventBus


@dataclass
class PendingAck:
    packet_id: int
    destination: str
    message: str
    timestamp: datetime
    status: str = "pending"  # "ack" | "nack" | "pending"


@dataclass(frozen=True)
class CommandSpec:
    aliases: tuple[str, ...]
    usage: str
    description: str
    handler: Callable[[str], bool | None]
 

class NodeSuggester(Suggester):
    def __init__(self, iface, node_target_commands: set[str]):
        super().__init__()
        self.iface = iface
        self.node_target_commands = {a.lower() for a in node_target_commands}

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None

        cmd, sep, remainder = value[1:].partition(" ")
        cmd = cmd.lower()
        if cmd not in self.node_target_commands or not sep:
            return None

        partial = remainder.lstrip('"').lower()
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
                    return f"/{cmd} {dest} "
        return None

# -- App ---------------------------------------------------------------------

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
    
    CORE_HELP_TIP = "  Tip: to copy text, use Shift+drag in a standard terminal"

    def __init__(self, port: str = None, host: str = None, feature_paths: list[str] = None, cli_args: list[str] = None):
        super().__init__()
        self.title = "Voltur's Meshtastic Interface"
        self.cli_args = cli_args or [] # Store CLI args for features to access if needed
        self.bus      = MeshEventBus()

        self._core_specs: list[CommandSpec] = []
        self._core_commands: dict[str, CommandSpec] = {}
        self._commands: dict[str, Callable[[str], None]] = {}
        self._sendto_aliases: set[str] = {"sendto"}
        self._node_target_commands: set[str] = {"sendto"}
        self._feature_path_set: set[str] = set()
        self._feature_paths_loaded: list[str] = []
        self._feature_instances: list[object] = []
        self._feature_cli_args_by_path: dict[str, list[str]] = {}
        self._feature_by_path: dict[str, object] = {}
        self._feature_command_map: dict[object, dict[str, Callable]] = {}
        self._feature_completion_map: dict[object, dict[str, str]] = {}
        self._node_cache: dict[str, dict] = {}
        self._ack_log: dict[int, PendingAck] = {}

        self.iface = self._connect(port=port, host=host)

        # Inject worker helpers into the bus now that we have self
        self.bus._run_worker       = lambda fn, **kw: self.run_worker(fn, thread=True, exclusive=False, **kw)
        self.bus._call_from_thread = self.call_from_thread

        # Load features after iface exists but ui_write isn't ready yet —
        # features that call ui_write at init time will queue via call_from_thread.
        self._register_core_commands()
        for path in (feature_paths or []):
            self._register_feature(path, announce_ui=False)

    def _register_core_commands(self) -> None:
        self._core_specs = [
            CommandSpec(
                aliases=("send",),
                usage="/send MSG",
                description="send a message to the mesh",
                handler=self._cmd_send,
            ),
            CommandSpec(
                aliases=("sendto","msg", "w"),
                usage='/sendto "NODE" MSG',
                description="send a message to a specific node",
                handler=lambda args: self._cmd_sendto(f"/sendto {args}"),
            ),
            CommandSpec(
                aliases=("nodes", "nodelist"),
                usage="/nodes",
                description="show nodes in the mesh",
                handler=lambda _args: self._cmd_nodes(),
            ),
            CommandSpec(
                aliases=("info", "nodeinfo", "nodeInfo"),
                usage="/info",
                description="show info about this node",
                handler=lambda _args: self._cmd_info(),
            ),
            CommandSpec(
                aliases=("help", "h"),
                usage="/help",
                description="show command help",
                handler=lambda _args: self._cmd_help(),
            ),
            CommandSpec(
                aliases=("feature",),
                usage="/feature load PATH [ARGS...] | reload TARGET [ARGS...]",
                description="manage feature plugins",
                handler=lambda args: self._cmd_feature(args),
            ),
            CommandSpec(
                aliases=("q", "quit", "exit"),
                usage="/quit",
                description="exit the app",
                handler=lambda _args: self._cmd_quit(),
            ),
        ]

        self._core_commands = {}
        for spec in self._core_specs:
            for alias in spec.aliases:
                self._core_commands[alias] = spec

        self._sendto_aliases = {
            alias
            for spec in self._core_specs
            if "sendto" in spec.aliases
            for alias in spec.aliases
        } or {"sendto"}
        self._node_target_commands = set(self._sendto_aliases)

    def _rebuild_completion_sets(self) -> None:
        commands = set(self._sendto_aliases)
        for completion_map in self._feature_completion_map.values():
            for cmd_name, kind in completion_map.items():
                if kind == "node_target":
                    commands.add(cmd_name.lower())
        self._node_target_commands = commands

    def load_feature(self, path: str, cli_args: list[str] | None = None):
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
            instance = feature_class(
                ui_write=self.ui_write,
                iface=self.iface,
                bus=self.bus,
                cli_args=cli_args if cli_args is not None else self.cli_args,
            )
            print(f"[feature] Loaded {feature_class.__name__} from {path}")
            return instance
        except Exception as e:
            print(f"[feature] Failed to instantiate {feature_class.__name__}: {e}", file=sys.stderr)
            return None

    def _normalize_feature_path(self, path: str) -> str:
        feature_path = Path(path).expanduser()
        if not feature_path.is_absolute():
            feature_path = Path.cwd() / feature_path
        return str(feature_path.resolve())

    def _register_feature(self, path: str, announce_ui: bool, feature_cli_args: list[str] | None = None) -> bool:
        normalized_path = self._normalize_feature_path(path)

        if normalized_path in self._feature_path_set:
            if announce_ui:
                self.ui_system_write(f"Feature already loaded: {normalized_path}")
            return False

        effective_cli_args = feature_cli_args if feature_cli_args is not None else self.cli_args

        feature = self.load_feature(normalized_path, cli_args=effective_cli_args)
        if not feature:
            if announce_ui:
                self.ui_write(f"[red]Feature load failed:[/] {normalized_path}")
            return False

        self._feature_instances.append(feature)
        feature_commands = feature.commands()
        feature_completions = feature.completions()
        self._commands.update(feature_commands)
        self._feature_path_set.add(normalized_path)
        self._feature_paths_loaded.append(normalized_path)
        self._feature_by_path[normalized_path] = feature
        self._feature_cli_args_by_path[normalized_path] = list(effective_cli_args)
        self._feature_command_map[feature] = feature_commands
        self._feature_completion_map[feature] = feature_completions
        self._rebuild_completion_sets()

        if announce_ui:
            self.ui_system_write(
                f"Loaded feature {feature.__class__.__name__} from {normalized_path}",
                log=True,
            )
        return True

    def _resolve_feature_target(self, target: str) -> tuple[str, object] | None:
        if not target:
            return None

        # /feature unload 2  (1-based index from /feature list)
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(self._feature_paths_loaded):
                path = self._feature_paths_loaded[idx - 1]
                feature = self._feature_by_path.get(path)
                if feature:
                    return path, feature

        # /feature unload features/foo.py
        normalized = self._normalize_feature_path(target)
        feature = self._feature_by_path.get(normalized)
        if feature:
            return normalized, feature

        # /feature unload FeatureClassName
        matches: list[tuple[str, object]] = []
        target_name = target.lower()
        for path in self._feature_paths_loaded:
            candidate = self._feature_by_path.get(path)
            if candidate and candidate.__class__.__name__.lower() == target_name:
                matches.append((path, candidate))

        if len(matches) == 1:
            return matches[0]

        return None

    def _unregister_feature(self, target: str, announce_ui: bool) -> bool:
        resolved = self._resolve_feature_target(target)
        if resolved is None:
            if announce_ui:
                self.ui_write(f"[red]Feature not found:[/] {target}")
                self.ui_system_write("Use /feature list to see loaded features")
            return False

        path, feature = resolved

        removed_handlers = self.bus.remove_handlers_for_owner(feature)

        feature_commands = self._feature_command_map.pop(feature, {})
        for cmd_name, handler in feature_commands.items():
            if self._commands.get(cmd_name) is handler:
                self._commands.pop(cmd_name, None)

        self._feature_completion_map.pop(feature, None)
        self._rebuild_completion_sets()

        try:
            feature.shutdown()
        except Exception as e:
            self.ui_write(f"[yellow]Feature shutdown error:[/] {e}")

        if feature in self._feature_instances:
            self._feature_instances.remove(feature)

        self._feature_by_path.pop(path, None)
        self._feature_cli_args_by_path.pop(path, None)
        self._feature_path_set.discard(path)
        if path in self._feature_paths_loaded:
            self._feature_paths_loaded.remove(path)

        if announce_ui:
            self.ui_system_write(
                f"Unloaded feature {feature.__class__.__name__} from {path} (removed {removed_handlers} handler(s))",
                log=True,
            )
        return True
        
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
            suggester=NodeSuggester(self.iface, self._node_target_commands),)
        
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
            count = utf8_len(msg)
        except ValueError:
            if val.startswith("/send "):
                count = utf8_len(val[len("/send "):])
            else:
                count = 0
        limit = 180
        color = "red" if count > limit else "yellow" if count > 150 else "gray"
        self.query_one("#char_counter", Label).update(f"[{color}]{count}/{limit}[/{color}]")

    # -- UI helpers ----------------------------------------------------------

    def ui_write(self, text: str, log: bool = False) -> None:
        self.messages.write(text)
        if log:
            self.bus.context_log.append(strip_markup(text))

    def ui_system_write(self, text: str, log: bool = False) -> None:
        self.ui_write(f"[dim]{text}[/dim]", log=log)

    def _format_timestamp(self, packet: dict) -> str:
        ts = packet.get("rxTime")
        if ts:
            return datetime.fromtimestamp(ts).strftime("[%H:%M]")
        return datetime.now().strftime("[%H:%M]")

    def _build_help(self) -> str:
        lines = ["Commands:"]
        for spec in self._core_specs:
            alias_text = ""
            if len(spec.aliases) > 1:
                alias_text = f" (aliases: {', '.join('/' + a for a in spec.aliases[1:])})"
            lines.append(f"  {spec.usage:24s} - {spec.description}{alias_text}")

        lines.append("")
        lines.append(self.CORE_HELP_TIP)

        if not self._feature_instances:
            lines.append("")
            lines.append("No features loaded.")
        else:
            for feature in self._feature_instances:
                extra = feature.help_text()
                if extra:
                    lines.append("")
                    lines.append(f"{feature.__class__.__name__} commands:")
                    lines.extend(extra)
        lines.append("")
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
            user = packet.get("decoded", {}).get("user", {})
            node_key = self._node_cache_key(packet, user)
            self.call_from_thread(self._handle_node_updated, node_key, user)
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

    def _node_cache_key(self, packet: dict, user: dict) -> str:
        """Return stable identity for NODEINFO cache and rename detection."""
        for key in ("id", "nodeId", "num"):
            value = user.get(key)
            if value not in (None, ""):
                return str(value)
        return str(packet.get("fromId", "unknown"))


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
        if not text:
            event.input.value = ""
            return

        if self._dispatch_input(text):
            event.input.value = ""

    def _dispatch_input(self, text: str) -> bool:
        if not text.startswith("/"):
            self.ui_system_write(f"Unknown command: {text}", log=True)
            self.ui_system_write(self._build_help(), log=True)
            return False

        cmd, _, args = text[1:].partition(" ")
        args = args.strip()

        core_spec = self._core_commands.get(cmd)
        if core_spec:
            return self._safe_execute(lambda: core_spec.handler(args), f"/{cmd}")

        if cmd in self._commands:
            return self._safe_execute(lambda: self._commands[cmd](args), f"/{cmd}")

        self.ui_write(f"[red]Unknown command:[/] /{cmd}")
        return False

    def _safe_execute(self, fn: Callable[[], bool | None], command_name: str) -> bool:
        """Execute a command handler and surface errors without crashing input handling."""
        try:
            result = fn()
            if isinstance(result, bool):
                return result
            return True
        except Exception as e:
            self.ui_write(f"[red]Command error in {command_name}:[/] {e}")
            return False

    def _cmd_send(self, msg: str) -> bool:
        if not msg.strip():
            self.ui_system_write("Usage: /send <message>")
            return False

        msg = truncate_for_mesh(msg)
        try:
            self.iface.sendText(msg, destinationId=BROADCAST_ADDR, wantAck=False)
        except Exception as e:
            self.ui_write(f"[red]Send failed:[/] {e}")
            return False

        self.ui_write(f"[bold cyan]You:[/] {msg}", log=True)
        self.bus.fire("on_send", BROADCAST_ADDR, msg)
        return True

    def _cmd_nodes(self) -> bool:
        self.ui_system_write(format_nodes(self.iface), log=True)
        return True

    def _cmd_info(self) -> bool:
        self.ui_system_write(format_node_info(self.iface), log=True)
        return True

    def _cmd_help(self) -> bool:
        self.ui_system_write(self._build_help(), log=True)
        return True

    def _cmd_feature(self, args: str) -> bool:
        try:
            parts = shlex.split(args)
        except ValueError as e:
            self.ui_write(f"[red]Parse error:[/] {e}")
            self.ui_system_write("Usage: /feature load PATH [ARGS...] | /feature unload TARGET | /feature reload TARGET [ARGS...] | /feature list")
            return False

        if not parts:
            self.ui_system_write("Usage: /feature load PATH [ARGS...] | /feature unload TARGET | /feature reload TARGET [ARGS...] | /feature list")
            return False

        subcmd = parts[0].lower()

        if subcmd in {"help", "?"}:
            self.ui_system_write("Usage: /feature load PATH [ARGS...] | /feature unload TARGET | /feature reload TARGET [ARGS...] | /feature list")
            return True

        if subcmd == "list":
            if not self._feature_paths_loaded:
                self.ui_system_write("No features loaded.")
                return True

            lines = ["Loaded features:"]
            for idx, path in enumerate(self._feature_paths_loaded, start=1):
                feature = self._feature_by_path.get(path)
                feature_name = feature.__class__.__name__ if feature else "UnknownFeature"
                arg_str = " ".join(shlex.quote(arg) for arg in self._feature_cli_args_by_path.get(path, []))
                if arg_str:
                    lines.append(f"  {idx}. {feature_name} - {path} [args: {arg_str}]")
                else:
                    lines.append(f"  {idx}. {feature_name} - {path}")
            self.ui_system_write("\n".join(lines), log=True)
            return True

        if subcmd == "load":
            if len(parts) < 2:
                self.ui_system_write("Usage: /feature load PATH [ARGS...]")
                return False
            feature_path = parts[1]
            passthrough_args = self.cli_args + parts[2:]
            return self._register_feature(feature_path, announce_ui=True, feature_cli_args=passthrough_args)

        if subcmd in {"unload", "disable"}:
            if len(parts) < 2:
                self.ui_system_write("Usage: /feature unload TARGET")
                self.ui_system_write("TARGET can be list index, path, or feature class name")
                return False
            return self._unregister_feature(parts[1], announce_ui=True)

        if subcmd == "reload":
            if len(parts) < 2:
                self.ui_system_write("Usage: /feature reload TARGET [ARGS...]")
                self.ui_system_write("TARGET can be list index, path, or feature class name")
                return False

            resolved = self._resolve_feature_target(parts[1])
            if resolved is None:
                self.ui_write(f"[red]Feature not found:[/] {parts[1]}")
                self.ui_system_write("Use /feature list to see loaded features")
                return False

            path, _ = resolved
            passthrough_args = self.cli_args + parts[2:] if len(parts) > 2 else self._feature_cli_args_by_path.get(path, self.cli_args)
            if not self._unregister_feature(parts[1], announce_ui=False):
                self.ui_write(f"[red]Feature reload failed (unload):[/] {parts[1]}")
                return False

            if not self._register_feature(path, announce_ui=False, feature_cli_args=passthrough_args):
                self.ui_write(f"[red]Feature reload failed (load):[/] {path}")
                return False

            feature = self._feature_by_path.get(path)
            feature_name = feature.__class__.__name__ if feature else "UnknownFeature"
            self.ui_system_write(f"Reloaded feature {feature_name} from {path}", log=True)
            return True

        self.ui_system_write("Usage: /feature load PATH [ARGS...] | /feature unload TARGET | /feature reload TARGET [ARGS...] | /feature list")
        return False

    def _cmd_quit(self) -> bool:
        self.exit()
        return True

    def _cmd_sendto(self, text: str) -> bool:
        try:
            raw_dest, msg = parse_sendto(text)
            if not msg.strip():
                self.ui_system_write('Usage: /sendto "<node name or id>" <message>')
                return False

            msg  = truncate_for_mesh(msg)
            dest = resolve_destination(raw_dest, self.iface)

            sent_packet = self.iface.sendText(msg, destinationId=dest, wantAck=True)
            packet_id = getattr(sent_packet, "id", None)

            if packet_id is not None:
                self._ack_log[packet_id] = PendingAck(
                    packet_id=packet_id,
                    destination=raw_dest,
                    message=msg,
                    timestamp=datetime.now()
                )
                self.ui_write(
                    f"[bold green][SENT -> {raw_dest} id={packet_id}][/bold green] {msg}", log=True
                )
            else:
                self.ui_write(f"[bold green][SENT -> {raw_dest}][/bold green] {msg}", log=True)

            self.bus.fire("on_send", dest, msg)
            return True

        except ValueError as e:
            self.ui_write(f"[red]{e}[/red]")
            self.ui_system_write('Usage: /sendto "<node name or id>" <message>')
            return False

        except Exception as e:
            self.ui_write(f"[red]Send failed:[/] {e}")
            return False


# -- Entry point -------------------------------------------------------------

def discover_feature_paths(features_dir: Path) -> list[str]:
    """Return sorted plugin paths from a features directory."""
    if not features_dir.exists() or not features_dir.is_dir():
        return []

    paths: list[str] = []
    for path in sorted(features_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        paths.append(str(path))
    return paths


def merge_feature_paths(auto_paths: list[str], explicit_paths: list[str]) -> list[str]:
    """Merge feature paths while preserving order and removing duplicates."""
    merged: list[str] = []
    seen: set[str] = set()

    for path in auto_paths + explicit_paths:
        if path not in seen:
            merged.append(path)
            seen.add(path)

    return merged

def main():
    parser = argparse.ArgumentParser(description="Voltur's Meshtastic Interface")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--port",    help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    group.add_argument("--host",    help="TCP hostname/IP for network-connected device")
    parser.add_argument(
        "--features",
        default="none",
        metavar="MODE",
        help="Feature loading mode: all or none (default)",
    )
    parser.add_argument(
        "--feature",
        action="append",
        default=[],
        metavar="PATH",
        help="Path to a feature plugin file (can be repeated)",
    )
    args, _unknown_args = parser.parse_known_args()

    mode = str(args.features).strip().lower()
    if mode not in {"all", "none"}:
        parser.error("--features must be 'all' or 'none'")

    features_dir = Path(__file__).resolve().parent / "features"
    auto_paths = discover_feature_paths(features_dir) if mode == "all" else []
    feature_paths = merge_feature_paths(auto_paths, args.feature)

    if mode == "all" and auto_paths:
        print(f"[feature] Auto-discovered {len(auto_paths)} feature(s) from {features_dir}")

    app = MeshChatApp(
        port=args.port,
        host=args.host,
        feature_paths=feature_paths,
        cli_args=sys.argv[1:],
    )

    try:
        app.run()
    finally:
        app.iface.close()
        for feature in app._feature_instances:
            feature.shutdown()


if __name__ == "__main__":
    main()
