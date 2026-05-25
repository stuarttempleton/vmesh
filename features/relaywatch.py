"""
features/relaywatch.py - Routing/ACK health monitor for vmesh.

Usage:
    python vmesh.py --port /dev/ttyUSB0 --feature features/relaywatch.py

Commands:
    /relay                 - show ACK/NACK summary
    /relay status          - same as /relay
    /relay recent [N]      - show most recent routing outcomes (default 10)
    /relay watch on|off    - live-print routing outcomes as they arrive
    /relay reset           - reset counters and history
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
from threading import Lock
from typing import Callable

from feature_base import MeshFeature


class RelayWatchFeature(MeshFeature):
    def __init__(self, ui_write: Callable, iface, bus):
        super().__init__(ui_write, iface, bus)

        self._lock = Lock()
        self._total = 0
        self._ack = 0
        self._nack = 0
        self._reasons: Counter[str] = Counter()
        self._history: deque[dict] = deque(maxlen=100)
        self._watch_enabled = False

        bus.on("on_routing", self._on_routing)

    def commands(self) -> dict[str, Callable]:
        return {
            "relay": self._cmd_relay,
        }

    def help_text(self) -> list[str]:
        watch_status = "on" if self._watch_enabled else "off"
        return [
            "  /relay                    - show ACK/NACK summary",
            "  /relay recent [N]         - show recent routing outcomes",
            f"  /relay watch on|off       - live routing output (currently {watch_status})",
            "  /relay reset              - clear relay stats",
        ]

    def _on_routing(self, packet: dict) -> None:
        decoded = packet.get("decoded", {})
        routing = decoded.get("routing", {})

        request_id = decoded.get("requestId")
        reason = str(routing.get("errorReason", "NONE"))
        success = reason == "NONE"
        from_id = str(packet.get("fromId", "unknown"))

        ts = packet.get("rxTime")
        record_ts = datetime.fromtimestamp(ts) if ts else datetime.now()
        record = {
            "timestamp": record_ts,
            "request_id": request_id,
            "reason": reason,
            "success": success,
            "from_id": from_id,
        }

        with self._lock:
            self._total += 1
            if success:
                self._ack += 1
            else:
                self._nack += 1
            self._reasons[reason] += 1
            self._history.append(record)
            watch_enabled = self._watch_enabled

        if watch_enabled:
            status = "ACK" if success else f"NACK:{reason}"
            req = request_id if request_id is not None else "-"
            msg = f"[dim][RelayWatch][/dim] {record_ts.strftime('%H:%M:%S')} req={req} from={from_id} {status}"
            self.bus.call_from_thread(self.ui_write, msg)

    def _cmd_relay(self, args: str) -> None:
        parts = args.strip().split()
        if not parts or parts[0].lower() in ("status", "summary"):
            self._show_summary()
            return

        subcmd = parts[0].lower()

        if subcmd == "recent":
            count = 10
            if len(parts) > 1:
                try:
                    count = int(parts[1])
                except ValueError:
                    self.ui_write("[yellow][RelayWatch][/yellow] Usage: /relay recent [N]")
                    return
            count = max(1, min(count, 50))
            self._show_recent(count)
            return

        if subcmd == "watch":
            if len(parts) < 2:
                state = "on" if self._watch_enabled else "off"
                self.ui_write(f"[RelayWatch] watch is currently [bold]{state}[/bold]. Usage: /relay watch on|off")
                return
            mode = parts[1].lower()
            if mode == "on":
                with self._lock:
                    self._watch_enabled = True
                self.ui_write("[green][RelayWatch][/green] Live watch enabled.")
            elif mode == "off":
                with self._lock:
                    self._watch_enabled = False
                self.ui_write("[yellow][RelayWatch][/yellow] Live watch disabled.")
            else:
                self.ui_write("[yellow][RelayWatch][/yellow] Usage: /relay watch on|off")
            return

        if subcmd == "reset":
            with self._lock:
                self._total = 0
                self._ack = 0
                self._nack = 0
                self._reasons.clear()
                self._history.clear()
            self.ui_write("[green][RelayWatch][/green] Stats reset.")
            return

        self.ui_write("[yellow][RelayWatch][/yellow] Usage: /relay [status|recent [N]|watch on|off|reset]")

    def _show_summary(self) -> None:
        with self._lock:
            total = self._total
            ack = self._ack
            nack = self._nack
            reasons = dict(self._reasons)

        if total == 0:
            self.ui_write("[RelayWatch] No routing packets seen yet.")
            return

        ack_rate = (ack / total) * 100.0
        lines = [
            "[bold]RelayWatch Summary[/bold]",
            f"  Total routing packets: {total}",
            f"  ACK: {ack} | NACK: {nack} | ACK rate: {ack_rate:.1f}%",
        ]

        if reasons:
            top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:5]
            lines.append("  Reasons (top): " + ", ".join(f"{k}={v}" for k, v in top))

        self.ui_write("\n".join(lines))

    def _show_recent(self, count: int) -> None:
        with self._lock:
            recent = list(self._history)[-count:]

        if not recent:
            self.ui_write("[RelayWatch] No recent routing events.")
            return

        lines = [f"[bold]RelayWatch Recent ({len(recent)})[/bold]"]
        for item in recent:
            status = "ACK" if item["success"] else f"NACK:{item['reason']}"
            req = item["request_id"] if item["request_id"] is not None else "-"
            lines.append(
                f"  {item['timestamp'].strftime('%H:%M:%S')} req={req} from={item['from_id']} {status}"
            )

        self.ui_write("\n".join(lines))
