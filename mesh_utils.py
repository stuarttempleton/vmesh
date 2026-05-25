"""
mesh_utils.py — Meshtastic utility functions (no UI dependency)
"""

import time
import shlex
from tzlocal import get_localzone


BROADCAST_ADDR = 0xFFFFFFFF
MAX_MESSAGE_LEN = 180

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


def get_posix_tz() -> str:
    tz_name = str(get_localzone())
    return POSIX_TZ.get(tz_name, "UTC0")


def set_timezone(interface, log_fn=None) -> None:
    """Sync time and update tzdef on the device if it has changed. Reboots if needed."""
    interface.localNode.setTime(int(time.time()))
    current_tz = interface.localNode.localConfig.device.tzdef
    desired_tz = get_posix_tz()

    if current_tz != desired_tz:
        if log_fn:
            log_fn(f"[TZ] Updating tzdef from '{current_tz}' to '{desired_tz}', rebooting...")
        interface.localNode.localConfig.device.tzdef = desired_tz
        interface.localNode.writeConfig("device")
        interface.localNode.reboot()


def get_node_display(interface, node_id: str) -> str:
    """Return 'LongName (!hexid)' for a node, or just '!hexid' if not found."""
    node = (interface.nodes or {}).get(node_id, {})
    user = node.get("user", {})
    long_name = user.get("longName")
    return f"{long_name} ({node_id})" if long_name else node_id


def format_node_info(interface) -> str:
    info = interface.getMyNodeInfo()
    return (
        f"\nNode ID   : {info.get('num', 'N/A')}\n"
        f"Long name : {info.get('user', {}).get('longName', 'N/A')}\n"
        f"Short name: {info.get('user', {}).get('shortName', 'N/A')}\n"
        f"Hardware  : {info.get('user', {}).get('hwModel', 'N/A')}\n"
        f"Timezone  : {interface.localNode.localConfig.device.tzdef}\n"
        f"Nodes     : {len(interface.nodes or {})}"
    )

def format_node_banner(interface) -> str:
    if interface is None:
        return "No device connected"
    
    my_node   = interface.getMyNodeInfo() or {}
    user      = my_node.get("user", {})
    device    = my_node.get("deviceMetrics", {})
    
    node_id   = user.get("id", "!unknown")
    long_name = user.get("longName", "unknown")
    battery   = device.get("batteryLevel", "?")
    ch_util   = device.get("channelUtilization", 0.0)
    air_util  = device.get("airUtilTx", 0.0)
    #snr       = my_node.get("snr", "?")
    
    battery_str = f"{battery}%" if battery != "?" else "?"
    
    return (
        f"{node_id} | {long_name} | "
        f"bat: {battery_str} | "
        f"ch: {ch_util:.2f}% | air: {air_util:.2f}%"
    )


def format_nodes(interface) -> str:
    nodes = interface.nodes or {}
    lines = [f"\nNodes in mesh: {len(nodes)}"]
    for node_id, node in nodes.items():
        user = node.get("user", {})
        snr  = node.get("snr", "?")
        hops = node.get("hopsAway", "?")
        lines.append(
            f"  {node_id:20s}  name={user.get('longName', '?'):20s}  SNR={snr}  HOPS={hops}"
        )
    return "\n".join(lines)


def resolve_destination(raw_dest: str, interface):
    """
    Resolve a destination string to a node ID or integer address.
    Accepts: !hexid, 0xHEX, decimal int, or longName/shortName (case-insensitive).
    Raises ValueError on ambiguity or not found.
    """
    raw_dest = raw_dest.strip()

    if not raw_dest:
        raise ValueError("Missing destination")

    if raw_dest.startswith("!"):
        return raw_dest

    if raw_dest.lower().startswith("0x"):
        return int(raw_dest, 16)

    if raw_dest.isdigit():
        return int(raw_dest)

    search = raw_dest.lower()
    matches = []

    for node_id, node in (interface.nodes or {}).items():
        user = node.get("user", {})
        long_name  = str(user.get("longName",  "")).lower()
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


def truncate_for_mesh(text: str, max_bytes: int = MAX_MESSAGE_LEN) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def parse_sendto(text: str):
    """
    Parse a /sendto command string.
    Returns (raw_dest, message) or raises ValueError.
    """
    parts = shlex.split(text)
    if len(parts) < 3:
        raise ValueError("Missing destination or message")
    raw_dest = parts[1]
    msg = " ".join(parts[2:]).strip()
    return raw_dest, msg
