#!/usr/bin/env python3
"""
Meshtastic Hello World
======================
A bare-bones example for connecting to a Meshtastic device,
reading node info, and sending/receiving messages.

Install dependency:
    pip install meshtastic

Usage:
    # Auto-detect USB device (most common)
    python meshtastic_hello.py

    # Specify a serial port
    python meshtastic_hello.py --port /dev/ttyUSB0

    # Connect over TCP (e.g. to a device on your LAN)
    python meshtastic_hello.py --host 192.168.1.50

    # Send a message instead of just listening
    python meshtastic_hello.py --send "Hello mesh!"
"""

import os
from datetime import datetime
import argparse
import time
from tzlocal import get_localzone
import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from voltllmclient import LLMClient
from voltllmclient import LLMConversation

isProcessing = False
packetQueue = []

packet_response_prompt = "You are a helpful assistant for responding to messages received on a Meshtastic mesh network. Generate a friendly and concise response to the following message:\n\n{}"
llm_conversations = {} # format: {node_id: LLMConversation}
BROADCAST_ADDR = 0xFFFFFFFF

# -- Packet Queue and LLM Processing (optional) ----------------------------------------------
def process_packet_queue():
    global isProcessing
    isProcessing = True
    while packetQueue:
        packet = packetQueue.pop(0)
        print(f"[LLM] Processing packet from {packet.get('fromId', 'unknown')}")
        if packet.get('fromId') not in llm_conversations:
            llm_conversations[packet.get('fromId')] = LLMConversation(base_url="http://localhost:11434", model="gemma3:4b", system_prompt=packet_response_prompt)
        response = llm_conversations[packet.get('fromId')].send_with_full_context(f"Received packet: {packet}")
        # iface.sendText(response, destination=packet.get('fromId'))
        print(f"[LLM] Generated response: {response}")
    isProcessing = False

def close_all_conversations():
    for key in llm_conversations.keys():
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        # Windows can't handle certain characters in filenames
        safe_persona = key if key is not None else "unknown_sender"
        for ch in [':', '/', '\\', '*', '?', '"', '<', '>', '|']:
            safe_persona = safe_persona.replace(ch, '_')
        filename = f"{date_str}_{safe_persona}.json"

        llm_conversations[key].save_transcript(filename)

# ── Callbacks ────────────────────────────────────────────────────────────────

def on_receive(packet, interface):
    """Called whenever a packet arrives."""
    decoded = packet.get("decoded", {})
    msg = decoded.get("text", None)
    if msg:
        sender = packet.get("fromId", "unknown")
        print(f"[MESSAGE] {sender}: {msg}")
        packetQueue.append(packet)
"""     else:
        # Non-text packet (position, telemetry, etc.)
        portnum = decoded.get("portnum", "UNKNOWN_APP")
        print(f"[PACKET ] portnum={portnum} from={packet.get('fromId', '?')}") """


def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Called once the device is connected and ready."""
    print("[CONNECTED] Device is ready.\n")

    # Handle setting time and timezone. (tzdata requires reboot)
    interface.localNode.setTime(int(time.time()))
    current_tz = interface.localNode.localConfig.device.tzdef
    desired_tz = get_posix_tz()
    
    if current_tz != desired_tz:
        print(f"[TZ] Updating tzdef from '{current_tz}' to '{desired_tz}', rebooting...")
        interface.localNode.localConfig.device.tzdef = desired_tz
        interface.localNode.writeConfig("device")
        interface.localNode.reboot()

    info = interface.getMyNodeInfo()
    print(f"  Node ID   : {info.get('num', 'N/A')}")
    print(f"  Long name : {info.get('user', {}).get('longName', 'N/A')}")
    print(f"  Short name: {info.get('user', {}).get('shortName', 'N/A')}")
    print(f"  Hardware  : {info.get('user', {}).get('hwModel', 'N/A')}")
    print(f"  Timezone  : {interface.localNode.localConfig.device.tzdef}")

    nodes = interface.nodes or {}
    print(f"\n  Nodes in mesh: {len(nodes)}")
    for node_id, node in nodes.items():
        user = node.get("user", {})
        snr  = node.get("snr", "?")
        hops = node.get("hopsAway", "?")
        print(f"    {node_id:20s}  name={user.get('longName','?'):20s}  SNR={snr}  HOPS={hops}")

    print()


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

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Meshtastic hello world")
    group  = parser.add_mutually_exclusive_group()
    group.add_argument("--port", help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    group.add_argument("--host", help="TCP hostname/IP for network-connected device")
    parser.add_argument("--send", metavar="MSG", help="Send a broadcast message then exit")
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

    try:
        if args.send:
            # Wait a moment for the connection callback to fire
            time.sleep(2)
            print(f"[SENDING ] {args.send!r}")
            iface.sendText(args.send)
            time.sleep(1)                       # give it time to transmit
        else:
            print("Listening for packets… (Ctrl-C to quit)\n")
            while True:
                if not isProcessing and packetQueue:
                    process_packet_queue()
                else:
                    time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        iface.close()
        close_all_conversations()


if __name__ == "__main__":
    main()
