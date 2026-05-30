"""
features/meshpulse.py - Lightweight mesh activity dashboard for vmesh.

Usage:
    python vmesh.py --port /dev/ttyUSB0 --feature features/meshpulse.py

Commands:
    /pulse                 - show live mesh activity summary
    /pulse status          - same as /pulse
    /pulse top [N]         - top talkers by inbound packet count
    /pulse watch on|off    - emit compact periodic activity heartbeat
    /pulse reset           - reset counters and history
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timedelta
from threading import Lock
from typing import Callable

from feature_base import MeshFeature


class MeshPulseFeature(MeshFeature):
    def __init__(self, ui_write: Callable, iface, bus, cli_args=None):
        super().__init__(ui_write, iface, bus, cli_args)

        self._lock = Lock()
        self._started_at = datetime.now()

        self._inbound_packets = 0
        self._outbound_packets = 0
        self._inbound_bytes = 0
        self._outbound_bytes = 0

        self._seen_senders: set[str] = set()
        self._sender_counts: Counter[str] = Counter()

        self._recent_inbound: deque[datetime] = deque(maxlen=2048)
        self._recent_outbound: deque[datetime] = deque(maxlen=2048)
        self._watch_enabled = False
        self._watch_last_emit: datetime | None = None
        
        # Use parsed args if available, otherwise default to 60 seconds
        watch_interval = self.args.pulse_watch_interval if self.args else 60
        self._watch_min_interval = timedelta(seconds=watch_interval)

        bus.on("on_connect", self._on_connect)
        bus.on("on_packet", self._on_packet)
        bus.on("on_send", self._on_send)

    def add_arguments(self, parser):
        """Add MeshPulse-specific CLI arguments."""
        parser.add_argument(
            '--pulse-watch-interval',
            type=int,
            default=60,
            help='Interval in seconds for pulse watch heartbeat (default: 60)'
        )
        return parser

    def commands(self) -> dict[str, Callable]:
        return {
            "pulse": self._cmd_pulse,
        }

    def help_text(self) -> list[str]:
        watch_status = "on" if self._watch_enabled else "off"
        return [
            "  /pulse                    - show live mesh activity summary",
            "  /pulse top [N]            - show top talkers by inbound packets",
            f"  /pulse watch on|off       - periodic heartbeat (currently {watch_status})",
            "  /pulse reset              - clear MeshPulse stats",
        ]

    def _on_connect(self, interface) -> None:
        self.ui_write("[green][MeshPulse][/green] Ready. Tracking session activity.")

    def _on_packet(self, packet: dict) -> None:
        sender = str(packet.get("fromId", "unknown"))
        text = packet.get("decoded", {}).get("text", "")
        payload_bytes = len(str(text).encode("utf-8"))

        now = datetime.now()
        with self._lock:
            self._inbound_packets += 1
            self._inbound_bytes += payload_bytes
            self._seen_senders.add(sender)
            self._sender_counts[sender] += 1
            self._recent_inbound.append(now)
            self._trim_recent_locked(now)
        self._maybe_emit_watch(now)

    def _on_send(self, destination, message) -> None:
        payload_bytes = len(str(message).encode("utf-8"))
        now = datetime.now()
        with self._lock:
            self._outbound_packets += 1
            self._outbound_bytes += payload_bytes
            self._recent_outbound.append(now)
            self._trim_recent_locked(now)
        self._maybe_emit_watch(now)

    def _maybe_emit_watch(self, now: datetime) -> None:
        with self._lock:
            if not self._watch_enabled:
                return

            if self._watch_last_emit and (now - self._watch_last_emit) < self._watch_min_interval:
                return

            self._trim_recent_locked(now)
            self._watch_last_emit = now
            in_5m = len(self._recent_inbound)
            out_5m = len(self._recent_outbound)
            in_total = self._inbound_packets
            out_total = self._outbound_packets
            senders = len(self._seen_senders)

        self.ui_write(
            f"[dim][MeshPulse][/dim] in/out last5m={in_5m}/{out_5m} total={in_total}/{out_total} senders={senders}"
        )

    def _trim_recent_locked(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=5)
        while self._recent_inbound and self._recent_inbound[0] < cutoff:
            self._recent_inbound.popleft()
        while self._recent_outbound and self._recent_outbound[0] < cutoff:
            self._recent_outbound.popleft()

    def _cmd_pulse(self, args: str) -> None:
        parts = args.strip().split()
        if not parts or parts[0].lower() in ("status", "summary"):
            self._show_summary()
            return

        subcmd = parts[0].lower()

        if subcmd == "top":
            n = 5
            if len(parts) > 1:
                try:
                    n = int(parts[1])
                except ValueError:
                    self.ui_write("[yellow][MeshPulse][/yellow] Usage: /pulse top [N]")
                    return
            n = max(1, min(n, 20))
            self._show_top_talkers(n)
            return

        if subcmd == "watch":
            if len(parts) < 2:
                state = "on" if self._watch_enabled else "off"
                self.ui_write(f"[MeshPulse] watch is currently [bold]{state}[/bold]. Usage: /pulse watch on|off")
                return

            mode = parts[1].lower()
            if mode == "on":
                with self._lock:
                    self._watch_enabled = True
                    self._watch_last_emit = None
                self.ui_write("[green][MeshPulse][/green] Watch enabled.")
            elif mode == "off":
                with self._lock:
                    self._watch_enabled = False
                self.ui_write("[yellow][MeshPulse][/yellow] Watch disabled.")
            else:
                self.ui_write("[yellow][MeshPulse][/yellow] Usage: /pulse watch on|off")
            return

        if subcmd == "reset":
            with self._lock:
                self._started_at = datetime.now()
                self._inbound_packets = 0
                self._outbound_packets = 0
                self._inbound_bytes = 0
                self._outbound_bytes = 0
                self._seen_senders.clear()
                self._sender_counts.clear()
                self._recent_inbound.clear()
                self._recent_outbound.clear()
                self._watch_last_emit = None
            self.ui_write("[green][MeshPulse][/green] Stats reset.")
            return

        self.ui_write("[yellow][MeshPulse][/yellow] Usage: /pulse [status|top [N]|watch on|off|reset]")

    def _show_summary(self) -> None:
        now = datetime.now()
        with self._lock:
            self._trim_recent_locked(now)
            uptime = now - self._started_at
            inbound_packets = self._inbound_packets
            outbound_packets = self._outbound_packets
            inbound_bytes = self._inbound_bytes
            outbound_bytes = self._outbound_bytes
            senders = len(self._seen_senders)
            inbound_5m = len(self._recent_inbound)
            outbound_5m = len(self._recent_outbound)

        total_packets = inbound_packets + outbound_packets
        total_bytes = inbound_bytes + outbound_bytes

        lines = [
            "[bold]MeshPulse Summary[/bold]",
            f"  Session uptime: {self._format_duration(uptime)}",
            f"  Packets in/out: {inbound_packets}/{outbound_packets} (total {total_packets})",
            f"  Bytes in/out:   {inbound_bytes}/{outbound_bytes} (total {total_bytes})",
            f"  Unique senders seen: {senders}",
            f"  Last 5 min in/out: {inbound_5m}/{outbound_5m}",
        ]
        self.ui_write("\n".join(lines))

    def _show_top_talkers(self, n: int) -> None:
        with self._lock:
            top = self._sender_counts.most_common(n)

        if not top:
            self.ui_write("[MeshPulse] No inbound packets seen yet.")
            return

        lines = [f"[bold]MeshPulse Top Talkers ({len(top)})[/bold]"]
        for idx, (sender, count) in enumerate(top, start=1):
            lines.append(f"  {idx:>2}. {sender} - {count} packets")

        self.ui_write("\n".join(lines))

    def _format_duration(self, delta: timedelta) -> str:
        seconds = int(delta.total_seconds())
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
