#!/usr/bin/env python3
"""
Voltur's Meshtastic Interface
This application connects to a Meshtastic device (via serial or TCP), listens for incoming messages, and provides a terminal-based UI for viewing messages and sending new ones. It also includes optional integration with a local LLM (e.g. Gemma) to auto-respond to received messages.
Features:
- Connect to Meshtastic device via serial port or TCP
- Display incoming messages with sender info
- Send messages to the entire mesh or specific nodes
- Show info about the local node and other nodes in the mesh
- Optional: Auto-generate responses to received messages using an LLM
Usage:
- Run the app with either --port or --host to connect to your device:
  python vmesh.py --port /dev/ttyUSB0
  python vmesh.py --host    
"""

import time
import queue
import argparse
from datetime import datetime
from tzlocal import get_localzone
import shlex

from textual.app import App, ComposeResult
from textual.widgets import RichLog, Input, Header, Footer

# Meshtastic imports
import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

# Optional: LLM integration for auto-responding to messages
from voltllmclient import LLMConversation
LLM_MODEL = "gemma3:4b"  # Change to your desired model, e.g. "gemma3:4b" or "gemma2:13b"
LLM_BASE_URL = "http://localhost:11434"  # Change if your LLM API is at a different URL

packet_queue = queue.Queue()  # Queue for incoming packets to be processed by LLM
ui_queue = queue.Queue()      # Queue for UI updates from other threads (e.g. packet processing)

BROADCAST_ADDR = 0xFFFFFFFF
BOT_NAME = "@sigyl"
AUTO_REPLY_ENABLED = False
MAX_MESSAGE_LEN = 180
MAX_TRANSCRIPT_MESSAGES = 50

packet_response_prompt = f"""
You are an assistant communicating over a Meshtastic mesh radio network.

Rules:
- Keep responses concise and bandwidth-efficient.
- Prefer 1 short sentence.
- Hard limit: {MAX_MESSAGE_LEN} characters maximum.
- Avoid markdown, lists, tables, or decorative formatting.
- Avoid emojis unless the user uses them first.
- Avoid assistant-style phrases like "Certainly!" or "I'd be happy to help."
- If unsure, say so briefly.
- Be conversational, helpful, and direct.
- Assume messages may be relayed over slow or unreliable radio links.

You are participating in a lightweight radio conversation, not writing an article.
""".strip()
local_chat_system_prompt = """
You are a local assistant integrated into a Meshtastic terminal client.

Your role is to help the operator:
- understand mesh activity
- troubleshoot Meshtastic behavior
- analyze packets and node state
- summarize recent conversations
- draft concise mesh-friendly replies
- assist with radio/network operations
- help debug this application

Guidelines:
- Be concise but informative.
- Prefer practical answers over generic explanations.
- Assume the user is technical.
- You may reference recent terminal/chat context provided to you.
- Distinguish clearly between facts, assumptions, and suggestions.
- When discussing mesh transmissions, prefer low-bandwidth solutions.
- Avoid excessive formatting or markdown unless helpful.
- If context is incomplete, say what additional information would help.

You are not speaking over the mesh directly unless explicitly instructed.
You are assisting locally inside the terminal application.
""".strip()
llm_conversations = {} # format: {node_id: LLMConversation}


# -- Packet Queue and LLM Processing (optional) ----------------------------------------------
def close_all_conversations():
    for key in llm_conversations.keys():
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        # Windows can't handle certain characters in filenames
        safe_persona = key if key is not None else "unknown_sender"
        for ch in [':', '/', '\\', '*', '?', '"', '<', '>', '|']:
            safe_persona = safe_persona.replace(ch, '_')
        filename = f"{date_str}_{safe_persona}.json"

        llm_conversations[key].save_transcript(filename)

# Callbacks for Meshtastic events

def on_receive(packet, interface):
    """Called whenever a packet arrives."""
    decoded = packet.get("decoded", {})
    msg = decoded.get("text", None)
    if msg:
        packet_queue.put(packet)


def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Called once the device is connected and ready."""
    ui_queue.put("[bold green][CONNECTED][/bold green] Device is ready.")

    # Set time and timezone (tzdata changes require reboot)
    set_timezone(interface)

    # Print node info and existing nodes in the mesh
    ui_queue.put(f"[dim]{node_info(interface)}[/dim]")
    ui_queue.put("\n[dim]Type /help to show commands[/dim]")


def set_timezone(interface):
    # Handle setting time and timezone. (tzdata requires reboot)
    interface.localNode.setTime(int(time.time()))
    current_tz = interface.localNode.localConfig.device.tzdef
    desired_tz = get_posix_tz()
    
    if current_tz != desired_tz:
        ui_queue.put(f"[TZ] Updating tzdef from '{current_tz}' to '{desired_tz}', rebooting...")
        interface.localNode.localConfig.device.tzdef = desired_tz
        interface.localNode.writeConfig("device")
        interface.localNode.reboot()


def node_info(interface):
    info = interface.getMyNodeInfo()
    node_info = f"""
Node ID   : {info.get('num', 'N/A')}
Long name : {info.get('user', {}).get('longName', 'N/A')}
Short name: {info.get('user', {}).get('shortName', 'N/A')}
Hardware  : {info.get('user', {}).get('hwModel', 'N/A')}
Timezone  : {interface.localNode.localConfig.device.tzdef}
Nodes: {len(interface.nodes or {})}"""
    return node_info


def show_nodes(interface):
    nodes = interface.nodes or {}
    nodes_info = f"\nNodes in mesh: {len(nodes)}\n"
    for node_id, node in nodes.items():
        user = node.get("user", {})
        snr  = node.get("snr", "?")
        hops = node.get("hopsAway", "?")
        nodes_info += f"  {node_id:20s}  name={user.get('longName','?'):20s}  SNR={snr}  HOPS={hops}\n"
    return nodes_info


POSIX_TZ = {
    "America/New_York":    "EST5EDT,M3.2.0,M11.1.0",
    "America/Chicago":     "CST6CDT,M3.2.0,M11.1.0",
    "America/Denver":      "MST7MDT,M3.2.0,M11.1.0",
    "America/Phoenix":     "MST7",
    "America/Los_Angeles": "PST8PDT,M3.2.0,M11.1.0",
    "America/Anchorage":   "AKST9AKDT,M3.2.0,M11.1.0",
    "America/Honolulu":    "HST10",
    "Europe/London":       "GMT0BST,M3.5.0/1,M10.5.0",
    "Europe/Paris":        "CET-1CEST,M3.5.0,M10.5.0/3",
    "Europe/Berlin":       "CET-1CEST,M3.5.0,M10.5.0/3",
    "Asia/Tokyo":          "JST-9",
    "Asia/Shanghai":       "CST-8",
    "Australia/Sydney":    "AEST-10AEDT,M10.1.0,M4.1.0/3",
}

def get_posix_tz():
    tz_name = str(get_localzone())
    return POSIX_TZ.get(tz_name, "UTC0")

def resolve_destination(raw_dest, interface):
    raw_dest = raw_dest.strip()

    if not raw_dest:
        raise ValueError("Missing destination")

    # Meshtastic-style node ID, e.g. !cafebabe
    if raw_dest.startswith("!"):
        return raw_dest

    # Hex number, e.g. 0x1234abcd
    if raw_dest.lower().startswith("0x"):
        return int(raw_dest, 16)

    # Decimal node number
    if raw_dest.isdigit():
        return int(raw_dest)

    # Name lookup: longName or shortName, case-insensitive
    search = raw_dest.lower()
    matches = []

    for node_id, node in (interface.nodes or {}).items():
        user = node.get("user", {})
        long_name = str(user.get("longName", "")).lower()
        short_name = str(user.get("shortName", "")).lower()

        if search in (long_name, short_name):
            matches.append((node_id, user))

    if len(matches) == 1:
        return matches[0][0]

    if len(matches) > 1:
        names = ", ".join(
            f"{node_id} ({user.get('longName', '?')}/{user.get('shortName', '?')})"
            for node_id, user in matches
        )
        raise ValueError(f"Ambiguous destination: {names}")

    raise ValueError(f"Unknown destination: {raw_dest}")

def truncate_for_mesh(text, max_bytes=180):
    encoded = text.encode("utf-8")

    if len(encoded) <= max_bytes:
        return text

    encoded = encoded[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


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

    def __init__(self, iface, llm_model, llm_base_url):
        super().__init__()
        self.iface = iface
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url
        self.local_conversation = LLMConversation(
            base_url=self.llm_base_url,
            model=self.llm_model,
            system_prompt=local_chat_system_prompt
        )
        self.local_conversation.client.temperature = 0
        self.title = "Voltur's Meshtastic Interface"
    
    def on_mount(self):
        self.messages = self.query_one("#messages", RichLog)
        self.input = self.query_one(Input)

        self.input.focus()
        self.set_interval(0.1, self.drain_ui_queue)
        self.set_interval(0.1, self.drain_packet_queue)

    def ui_write(self, text, remember=True):
        self.messages.write(text)

        if remember:
            self.local_conversation.messages.append({
                "role": "system",
                "content": f"Terminal output:\n{text}",
            })
    
    def ui_system_write(self, text, remember=True):
        self.messages.write(f"[dim]{text}[/dim]")

        if remember:
            self.local_conversation.messages.append({
                "role": "system",
                "content": f"Terminal output:\n{text}",
            })
        
    def drain_ui_queue(self):
        while True:
            try:
                message = ui_queue.get_nowait()
            except queue.Empty:
                break

            self.ui_write(message)

    def drain_packet_queue(self):
        while True:
            try:
                packet = packet_queue.get_nowait()
            except queue.Empty:
                break

            self.process_packet(packet)

    def process_packet(self, packet):
        sender = packet.get("fromId", "unknown")
        msg = packet.get("decoded", {}).get("text")

        if msg:
            label = self.get_node_display(sender)
            timestamp = self.format_packet_time(packet)
            self.ui_write(f"[dim]{timestamp}[/dim] [bold magenta]{label}:[/] {msg}")

            self.run_worker(
                lambda: self.generate_llm_response(packet),
                thread=True,
                exclusive=False,
            )

    def get_node_display(self, node_id: str) -> str:
        """Returns 'LongName (!hexid)' or falls back to '!hexid'."""
        node = (self.iface.nodes or {}).get(node_id, {})
        user = node.get("user", {})
        long_name = user.get("longName")
        return f"{long_name} ({node_id})" if long_name else node_id

    def format_packet_time(self, packet: dict) -> str:
        ts = packet.get("rxTime")
        if ts:
            return datetime.fromtimestamp(ts).strftime("[%H:%M:%S]")
        return datetime.now().strftime("[%H:%M:%S]")
    
    def generate_llm_response(self, packet):
        sender = packet.get("fromId", "unknown")
        label = self.get_node_display(sender)

        try:

            raw_msg = packet.get("decoded", {}).get("text", "")

            if not self.should_auto_reply(packet, raw_msg):
                return
            
            msg = f"Received packet addressed to you as {BOT_NAME}: {packet}"
            
            if sender not in llm_conversations:
                llm_conversations[sender] = LLMConversation(
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    system_prompt=packet_response_prompt,
                )

            response = llm_conversations[sender].send_with_full_context(msg)

            response = truncate_for_mesh(response.strip())

            destination = packet.get("fromId")

            if AUTO_REPLY_ENABLED and destination:
                self.iface.sendText(
                    response,
                    destinationId=destination,
                    wantAck=True,
                )

                self.call_from_thread(
                    self.ui_write,
                    f"[bold green][SENT -> {destination}][/bold green] {response}"
                )
            else:
                self.call_from_thread(
                    self.ui_write,
                    f"[yellow][DRY RUN -> {destination}][/yellow] {response}"
                )

        except Exception as e:
            self.call_from_thread(
                self.ui_write,
                f"[red][LLM ERROR][/red] {e}"
            )

    def generate_local_llm_response(self, text):

        try:
            response = self.local_conversation.send_with_full_context(text)
            response = response.strip()

            self.call_from_thread(
                self.ui_write,
                f"[bold green]LLM:[/] {response}",
                False
            )
        except Exception as e:
            self.call_from_thread(
                self.ui_write,
                f"[red][LLM ERROR][/red] {e}",
                False
            )

    def should_auto_reply(self, packet, raw_msg):
        sender = packet.get("fromId")
        my_id = getattr(self.iface, "myInfo", None).my_node_num

        if sender == my_id:
            return False

        msg = raw_msg.strip().lower()

        short_name = (
            self.iface.getMyNodeInfo()
            .get("user", {})
            .get("shortName", "")
            .lower()
        )

        # Bot prompt aliases
        triggers = [
            f"@{short_name}",
            f"{short_name}:",
        ]

        return any(msg.startswith(trigger) for trigger in triggers)
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="messages", wrap=True, markup=True)
        yield Input(placeholder="Type /help for commands", max_length=180)
        yield Footer()

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""

        if text.startswith("/send "):
            msg = text[len("/send "):]
            msg = truncate_for_mesh(msg)
            self.iface.sendText(
                msg,
                destinationId=BROADCAST_ADDR,
                wantAck=False,
            )
            self.ui_write(f"[bold cyan]You:[/] {msg}")

        elif text.startswith("/sendto "):
            try:
                parts = shlex.split(text)

                if len(parts) < 3:
                    raise ValueError("Missing destination or message")

                raw_dest = parts[1]
                msg = " ".join(parts[2:]).strip()
                msg = truncate_for_mesh(msg)

                dest = resolve_destination(raw_dest, self.iface)

                sent_packet = self.iface.sendText(
                    msg,
                    destinationId=dest,
                    wantAck=True,
                )
                print(f"[RAW] Sent packet: {sent_packet}")

                packet_id = sent_packet.get("id", "?")

                self.ui_write(
                    f"[bold green][SENT -> {raw_dest} id={packet_id}][/bold green] {msg}"
                )

            except ValueError as e:
                self.ui_write(f"[red]{e}[/red]")
                self.ui_system_write(
                    'Usage: /sendto "<node name>" <message>'
                )

            except Exception as e:
                self.ui_write(f"[red]Parse error:[/] {e}")

        elif text == "/nodes":
            self.ui_system_write(show_nodes(self.iface))

        elif text == "/info":
            self.ui_system_write(node_info(self.iface))

        elif text in ("/q", "/quit", "/exit"):
            self.exit()
        
        elif text in ("/help", "/h"):
            help_text = "\n".join([
                "Commands:",
                "  /send MSG               - send a message to the mesh",
                "  /sendto NODE_ID MSG     - send a message to a specific node",
                "  /nodes                  - show nodes in the mesh",
                "  /info                   - show info about this node",
                "  /quit                   - exit the app",
                "",
                "  Tip: to copy text, use Shift+drag in a standard terminal",
            ])
            self.ui_system_write(help_text)
            
        elif text.startswith("/llm"):
            msg = text[len("/llm "):]
            self.ui_write(f"[bold cyan]You -> LLM:[/] {msg}", False)

            self.run_worker(
                lambda: self.generate_local_llm_response(msg),
                thread=True,
                exclusive=False,
            )
        
        elif text.startswith("/"):
            self.ui_write(f"[red]Unknown command:[/] {text}")

        


# Entry point for processing received packets and connection events
def main():
    global LLM_MODEL, LLM_BASE_URL
    
    parser = argparse.ArgumentParser(description="Voltur's Meshtastic Interface")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--port", help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    group.add_argument("--host", help="TCP hostname/IP for network-connected device")
    parser.add_argument("--llm-model", help="LLM model to use for auto-responses (e.g. gemma3:4b)")
    parser.add_argument("--llm-base-url", help="Base URL for LLM API (default: http://localhost:11434)")
    args = parser.parse_args()

    # Subscribe to events before opening the interface
    pub.subscribe(on_receive,    "meshtastic.receive")
    pub.subscribe(on_connection, "meshtastic.connection.established")

    # Open interface
    print("Connecting to Meshtastic device…")
    if args.host:
        iface = meshtastic.tcp_interface.TCPInterface(hostname=args.host)
    else:
        iface = meshtastic.serial_interface.SerialInterface(devPath=args.port)

    app = MeshChatApp(iface,
        llm_model=args.llm_model or LLM_MODEL,
        llm_base_url=args.llm_base_url or LLM_BASE_URL,)

    try:
        app.run()
    finally:
        iface.close()
        close_all_conversations()

# Entry point
if __name__ == "__main__":
    main()
