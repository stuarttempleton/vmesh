"""
features/trout.py - Extremely small demo plugin.

Command:
    /trout TARGET

Sends TARGET a direct message:
    *slaps TARGET with a wet trout*
"""

from __future__ import annotations

import shlex
from typing import Callable

from feature_base import MeshFeature
from mesh_utils import resolve_destination


class TroutFeature(MeshFeature):
    def commands(self) -> dict[str, Callable]:
        return {
            "trout": self._cmd_trout,
        }

    def completions(self) -> dict[str, str]:
        return {
            "trout": "node_target",
        }

    def help_text(self) -> list[str]:
        return [
            "  /trout TARGET            - direct message: *slaps TARGET with a wet trout*",
        ]

    def _cmd_trout(self, args: str) -> None:
        if not args.strip():
            self.ui_write("[yellow][Trout][/yellow] Usage: /trout TARGET")
            return

        try:
            parts = shlex.split(args)
        except ValueError as e:
            self.ui_write(f"[red][Trout parse error][/red] {e}")
            return

        if not parts:
            self.ui_write("[yellow][Trout][/yellow] Usage: /trout TARGET")
            return

        target_raw = parts[0]
        msg = f"*slaps {target_raw} with a wet trout*"

        try:
            dest = resolve_destination(target_raw, self.iface)
            self.iface.sendText(msg, destinationId=dest, wantAck=True)
            self.ui_write(f"[bold green][TROUT -> {target_raw}][/bold green] {msg}")
            self.bus.fire("on_send", dest, msg)
        except ValueError as e:
            self.ui_write(f"[red][Trout][/red] {e}")
        except Exception as e:
            self.ui_write(f"[red][Trout send failed][/red] {e}")
