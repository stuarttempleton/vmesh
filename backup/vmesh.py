#!/usr/bin/env python3
"""
Voltur's Meshtastic Interface

Connects to a Meshtastic device (serial or TCP), displays incoming messages
in a terminal UI, and optionally auto-responds using a local LLM.

Usage:
    python vmesh.py --port /dev/ttyUSB0
    python vmesh.py --host 192.168.1.100
"""

import argparse
from datetime import datetime

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from textual.app import App, ComposeResult
from textual.widgets import RichLog, Input, Header, Footer

from mesh_utils import (
    BROADCAST_ADDR,
    format_node_info,
    format_nodes,
    get_node_display,
    parse_sendto,
    resolve_destination,
    set_timezone,
    truncate_for_mesh,
)
from llm_utils import LLMManager, BOT_NAME, LLM_MODEL, LLM_BASE_URL

AUTO_REPLY_ENABLED = False

HELP_TEXT = "\n".join([
    "Commands:",
    "  /send MSG               - send a message to the mesh",
    "  /sendto NODE_ID MSG     - send a message to a specific node",
    "  /nodes                  - show nodes in the mesh",
    "  /info                   - show info about this node",
    "  /llm MSG                - chat with the local LLM assistant",
    "  /quit                   - exit the app",
    "",
    "  Tip: to copy text, use Shift+drag in a standard terminal",
    "       (VSCode's integrated terminal does not support this)",
])


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
    """

    def __init__(self, model: str, base_url: str, port: str = None, host: str = None):
        super().__init__()
        self.title = "Voltur's Meshtastic Interface"
        self.llm   = LLMManager(model=model, base_url=base_url)
        self.iface = self._connect(port=port, host=host)

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
        yield Input(placeholder="Type /help for commands", max_length=180)
        yield Footer()

    def on_mount(self) -> None:
        self.messages = self.query_one("#messages", RichLog)
        self.query_one(Input).focus()

    # -- UI helpers ----------------------------------------------------------

    def ui_write(self, text: str, remember: bool = True) -> None:
        self.messages.write(text)
        if remember:
            self.llm.local.messages.append({
                "role": "system",
                "content": f"Terminal output:\n{text}",
            })

    def ui_system_write(self, text, remember=True):
        self.ui_write(f"[dim]{text}[/dim]", remember=remember)

    def _format_timestamp(self, packet: dict) -> str:
        ts = packet.get("rxTime")
        if ts:
            return datetime.fromtimestamp(ts).strftime("[%H:%M:%S]")
        return datetime.now().strftime("[%H:%M:%S]")

    # -- Pubsub callbacks (called from Meshtastic background thread) ---------

    def _on_connection(self, interface, topic=pub.AUTO_TOPIC) -> None:
        self.call_from_thread(self.ui_write, "[bold green][CONNECTED][/bold green] Device is ready.")
        set_timezone(interface, log_fn=lambda msg: self.call_from_thread(self.ui_write, msg))
        self.call_from_thread(self.ui_system_write, format_node_info(interface))
        self.call_from_thread(self.ui_system_write, "\nType /help to show commands")

    def _on_receive(self, packet, interface) -> None:
        decoded = packet.get("decoded", {})
        if decoded.get("text"):
            self.call_from_thread(self._handle_incoming, packet)

    # -- Packet handling -----------------------------------------------------

    def _handle_incoming(self, packet: dict) -> None:
        sender = packet.get("fromId", "unknown")
        msg    = packet.get("decoded", {}).get("text", "")
        ts     = self._format_timestamp(packet)
        label  = get_node_display(self.iface, sender)

        self.ui_write(f"[dim]{ts}[/dim] [bold magenta]{label}:[/] {msg}")
        self.ui_system_write(f"[RAW] {packet}")

        self.run_worker(
            lambda: self._generate_mesh_reply(packet),
            thread=True,
            exclusive=False,
        )

    def _should_auto_reply(self, packet: dict, raw_msg: str) -> bool:
        sender = packet.get("fromId")
        my_num = getattr(self.iface, "myInfo", None).my_node_num

        if sender == my_num:
            return False

        msg        = raw_msg.strip().lower()
        short_name = (
            self.iface.getMyNodeInfo()
            .get("user", {})
            .get("shortName", "")
            .lower()
        )

        triggers = [f"@{short_name}", f"{short_name}:"]
        return any(msg.startswith(t) for t in triggers)

    # -- LLM workers (run in background thread) ------------------------------

    def _generate_mesh_reply(self, packet: dict) -> None:
        sender  = packet.get("fromId", "unknown")
        raw_msg = packet.get("decoded", {}).get("text", "")

        if not self._should_auto_reply(packet, raw_msg):
            return

        try:
            convo    = self.llm.get_node_conversation(sender)
            prompt   = f"Received packet addressed to you as {BOT_NAME}: {packet}"
            response = truncate_for_mesh(convo.send_with_full_context(prompt).strip())
            dest     = packet.get("fromId")

            if AUTO_REPLY_ENABLED and dest:
                self.iface.sendText(response, destinationId=dest, wantAck=True)
                self.call_from_thread(
                    self.ui_write,
                    f"[bold green][SENT -> {dest}][/bold green] {response}"
                )
            else:
                self.call_from_thread(
                    self.ui_write,
                    f"[yellow][DRY RUN -> {dest}][/yellow] {response}"
                )

        except Exception as e:
            self.call_from_thread(self.ui_write, f"[red][LLM ERROR][/red] {e}")

    def _generate_local_reply(self, text: str) -> None:
        try:
            response = self.llm.local.send_with_full_context(text).strip()
            self.call_from_thread(self.ui_write, f"[bold green]LLM:[/] {response}", False)
        except Exception as e:
            self.call_from_thread(self.ui_write, f"[red][LLM ERROR][/red] {e}", False)

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

        elif text == "/nodes":
            self.ui_system_write(format_nodes(self.iface))

        elif text == "/info":
            self.ui_system_write(format_node_info(self.iface))

        elif text in ("/help", "/h"):
            self.ui_system_write(HELP_TEXT)

        elif text in ("/q", "/quit", "/exit"):
            self.exit()

        elif text.startswith("/llm "):
            msg = text[len("/llm "):]
            self.ui_write(f"[bold cyan]You -> LLM:[/] {msg}", False)
            self.run_worker(lambda: self._generate_local_reply(msg), thread=True, exclusive=False)

        elif text.startswith("/"):
            self.ui_write(f"[red]Unknown command:[/] {text}")

    def _cmd_send(self, msg: str) -> None:
        msg = truncate_for_mesh(msg)
        self.iface.sendText(msg, destinationId=BROADCAST_ADDR, wantAck=False)
        self.ui_write(f"[bold cyan]You:[/] {msg}")

    def _cmd_sendto(self, text: str) -> None:
        try:
            raw_dest, msg = parse_sendto(text)
            msg  = truncate_for_mesh(msg)
            dest = resolve_destination(raw_dest, self.iface)

            sent_packet = self.iface.sendText(msg, destinationId=dest, wantAck=True)
            packet_id   = sent_packet.get("id", "?")

            self.ui_write(
                f"[bold green][SENT -> {raw_dest} id={packet_id}][/bold green] {msg}"
            )

        except ValueError as e:
            self.ui_write(f"[red]{e}[/red]")
            self.ui_system_write('Usage: /sendto "<node name or id>" <message>')

        except Exception as e:
            self.ui_write(f"[red]Parse error:[/] {e}")


# -- Entry point -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Voltur's Meshtastic Interface")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--port",        help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    group.add_argument("--host",        help="TCP hostname/IP for network-connected device")
    parser.add_argument("--llm-model",    default=LLM_MODEL,    help="LLM model name")
    parser.add_argument("--llm-base-url", default=LLM_BASE_URL, help="LLM API base URL")
    args = parser.parse_args()

    app = MeshChatApp(
        model=args.llm_model,
        base_url=args.llm_base_url,
        port=args.port,
        host=args.host,
    )

    try:
        app.run()
    finally:
        app.iface.close()
        app.llm.close_all()


if __name__ == "__main__":
    main()
