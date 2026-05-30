"""
features/nodeinfo.py - Query node details by target and field.

Usage:
    python vmesh.py --port /dev/ttyUSB0 --feature features/nodeinfo.py

Commands:
    /node TARGET                 - show summary for a node
    /node TARGET FIELD           - show one field for a node
    /node FIELD TARGET           - alternate form

Examples:
    /node "Alice"
    /node "Alice" gps
    /node gps "Alice"
    /node "Alice" battery
"""

from __future__ import annotations

import json
import shlex
from typing import Callable

from feature_base import MeshFeature
from mesh_utils import resolve_destination


class NodeInfoFeature(MeshFeature):
    def commands(self) -> dict[str, Callable]:
        return {
            "node": self._cmd_node,
        }

    def completions(self) -> dict[str, str]:
        return {
            "node": "node_target",
        }

    def help_text(self) -> list[str]:
        return [
            "  /node TARGET             - show summary for a node",
            "  /node TARGET FIELD       - show one field (gps|battery|snr|hops|name|short|id)",
            "  /node FIELD TARGET       - alternate form",
        ]

    def _cmd_node(self, args: str) -> None:
        if not args.strip():
            self.ui_write("[yellow][Node][/yellow] Usage: /node TARGET [FIELD]")
            return

        try:
            parts = shlex.split(args)
        except ValueError as e:
            self.ui_write(f"[red][Node parse error][/red] {e}")
            return

        target, field = self._parse_target_and_field(parts)
        if not target:
            self.ui_write("[yellow][Node][/yellow] Usage: /node TARGET [FIELD]")
            return

        node_id, node = self._resolve_node(target)
        if node is None:
            self.ui_write(f"[red][Node][/red] Node not found: {target}")
            return

        if field is None:
            self.ui_write(self._format_summary(node_id, node))
            return

        value = self._extract_field(node_id, node, field)
        if value is None:
            self.ui_write(f"[yellow][Node][/yellow] Unknown field '{field}'. Try gps, battery, snr, hops, name, short, id")
            return

        self.ui_write(f"[bold]Node {target} {field}[/bold]: {value}")

    def _parse_target_and_field(self, parts: list[str]) -> tuple[str | None, str | None]:
        if not parts:
            return None, None

        if len(parts) == 1:
            return parts[0], None

        known_fields = {
            "gps",
            "position",
            "battery",
            "snr",
            "hops",
            "name",
            "long",
            "short",
            "id",
            "hw",
            "model",
            "lat",
            "lon",
            "alt",
        }

        a = parts[0].lower()
        b = parts[1].lower()

        if a in known_fields and b not in known_fields:
            return parts[1], a

        if b in known_fields:
            return parts[0], b

        return parts[0], parts[1]

    def _resolve_node(self, target: str) -> tuple[str, dict | None]:
        nodes = self.iface.nodes or {}

        try:
            dest = resolve_destination(target, self.iface)
        except ValueError:
            return target, None

        if isinstance(dest, str):
            return dest, nodes.get(dest)

        for node_id, node in nodes.items():
            if node.get("num") == dest:
                return node_id, node

        return str(dest), None

    def _extract_field(self, node_id: str, node: dict, field: str):
        field = field.lower()
        user = node.get("user", {})
        pos = node.get("position", {})
        metrics = node.get("deviceMetrics", {})

        if field in {"name", "long"}:
            return user.get("longName") or "?"
        if field == "short":
            return user.get("shortName") or "?"
        if field == "id":
            return user.get("id") or node_id
        if field in {"hw", "model"}:
            return user.get("hwModel") or "?"
        if field == "snr":
            return node.get("snr", "?")
        if field == "hops":
            return node.get("hopsAway", "?")
        if field == "battery":
            return metrics.get("batteryLevel", "?")

        lat, lon, alt = self._position_values(pos)
        if field in {"gps", "position"}:
            if lat is None or lon is None:
                return "no position data"
            alt_text = f", alt={alt}m" if alt is not None else ""
            return f"lat={lat:.5f}, lon={lon:.5f}{alt_text}"
        if field == "lat":
            return lat if lat is not None else "?"
        if field == "lon":
            return lon if lon is not None else "?"
        if field == "alt":
            return alt if alt is not None else "?"

        return None

    def _position_values(self, pos: dict) -> tuple[float | None, float | None, int | None]:
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        alt = pos.get("altitude")

        if lat is None and "latitudeI" in pos:
            try:
                lat = float(pos["latitudeI"]) / 1e7
            except Exception:
                lat = None

        if lon is None and "longitudeI" in pos:
            try:
                lon = float(pos["longitudeI"]) / 1e7
            except Exception:
                lon = None

        return lat, lon, alt

    def _format_summary(self, node_id: str, node: dict) -> str:
        user = node.get("user", {})
        pos = node.get("position", {})
        metrics = node.get("deviceMetrics", {})

        lat, lon, alt = self._position_values(pos)
        pos_text = "unknown"
        if lat is not None and lon is not None:
            pos_text = f"lat={lat:.5f}, lon={lon:.5f}"
            if alt is not None:
                pos_text += f", alt={alt}m"

        summary = {
            "id": user.get("id") or node_id,
            "longName": user.get("longName", "?"),
            "shortName": user.get("shortName", "?"),
            "hwModel": user.get("hwModel", "?"),
            "snr": node.get("snr", "?"),
            "hopsAway": node.get("hopsAway", "?"),
            "battery": metrics.get("batteryLevel", "?"),
            "position": pos_text,
        }

        return "[bold]Node Summary[/bold]\n" + json.dumps(summary, indent=2)
