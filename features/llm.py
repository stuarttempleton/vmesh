"""
features/llm.py — LLM auto-reply and local assistant feature for vmesh.

Provides:
  /llm <msg>         — chat with the local LLM assistant
  /autoreply on|off  — toggle mesh auto-reply

Auto-reply fires when an incoming packet is addressed to this node's
short name (e.g. "@sigyl" or "sigyl: hello").

Dependencies:
  voltllmclient  (pip install voltllmclient or equivalent)

This file is intentionally self-contained so vmesh.py has zero LLM imports.
"""

from __future__ import annotations

from typing import Callable

from feature_base import MeshFeature
from mesh_utils import truncate_for_mesh

# -- Try importing the LLM client; fail gracefully if not installed ----------

try:
    from voltllmclient import LLMConversation
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False
    LLMConversation = None

# -- Defaults (overridable via feature args) ---------------------------------

DEFAULT_MODEL    = "gemma3:4b"
DEFAULT_BASE_URL = "http://localhost:11434"
MAX_MESSAGE_LEN  = 180

# -- System prompts ----------------------------------------------------------

_PACKET_PROMPT = f"""
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

_LOCAL_PROMPT = """
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


# -- Feature -----------------------------------------------------------------

class LLMFeature(MeshFeature):
    """
    LLM feature: local assistant + optional mesh auto-reply.

    Constructor args (passed via feature_args in vmesh.py):
        model    (str) : LLM model name,    default gemma3:4b
        base_url (str) : LLM API base URL,  default http://localhost:11434
    """

    def __init__(self, ui_write: Callable, iface, bus, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL):
        super().__init__(ui_write, iface, bus)

        self.model      = model
        self.base_url   = base_url
        self.auto_reply = False

        self._node_convos: dict[str, object] = {}  # node_id -> LLMConversation
        self._local_convo = None

        if not LLM_AVAILABLE:
            self.ui_write(
                "[yellow][LLM][/yellow] voltllmclient not installed — "
                "LLM feature loaded but inactive. Run: pip install voltllmclient"
            )
            return

        self._local_convo = LLMConversation(
            base_url=self.base_url,
            model=self.model,
            system_prompt=_LOCAL_PROMPT,
        )
        self._local_convo.client.temperature = 0

        # Subscribe to events
        bus.on("on_packet", self._on_packet)
        bus.on("on_connect", self._on_connect)

    # -- MeshFeature interface -----------------------------------------------

    def commands(self) -> dict[str, Callable]:
        return {
            "llm":       self._cmd_llm,
            "autoreply": self._cmd_autoreply,
        }

    def help_text(self) -> list[str]:
        status = "on" if self.auto_reply else "off"
        return [
            f"  /llm MSG                - chat with the local LLM assistant",
            f"  /autoreply on|off       - toggle mesh auto-reply (currently {status})",
        ]

    def shutdown(self) -> None:
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        for node_id, convo in self._node_convos.items():
            safe_id = node_id or "unknown"
            for ch in [':', '/', '\\', '*', '?', '"', '<', '>', '|']:
                safe_id = safe_id.replace(ch, '_')
            convo.save_transcript(f"{date_str}_{safe_id}.json")

    # -- Commands ------------------------------------------------------------

    def _cmd_llm(self, args: str) -> None:
        if not LLM_AVAILABLE or self._local_convo is None:
            self.ui_write("[yellow][LLM][/yellow] LLM client not available.")
            return
        if not args.strip():
            self.ui_write("[yellow][LLM][/yellow] Usage: /llm <message>")
            return

        self.ui_write(f"[bold cyan]You -> LLM:[/] {args}", False)

        from textual.app import App
        # We need to run this in a worker — get the app reference via iface context.
        # The app is passed through the bus so features don't import App directly.
        self.bus.run_worker(lambda: self._local_reply_worker(args))

    def _cmd_autoreply(self, args: str) -> None:
        arg = args.strip().lower()
        if arg == "on":
            self.auto_reply = True
            self.ui_write("[green][LLM][/green] Auto-reply enabled.")
        elif arg == "off":
            self.auto_reply = False
            self.ui_write("[yellow][LLM][/yellow] Auto-reply disabled.")
        else:
            status = "on" if self.auto_reply else "off"
            self.ui_write(f"[LLM] Auto-reply is currently [bold]{status}[/bold]. Use /autoreply on|off.")

    # -- Event handlers ------------------------------------------------------

    def _on_connect(self, interface) -> None:
        if LLM_AVAILABLE:
            self.ui_write(
                f"[green][LLM][/green] Ready — model: {self.model} @ {self.base_url}"
            )

    def _on_packet(self, packet: dict) -> None:
        if not LLM_AVAILABLE:
            return
        raw_msg = packet.get("decoded", {}).get("text", "")
        if self._should_reply(packet, raw_msg):
            self.bus.run_worker(lambda: self._mesh_reply_worker(packet))

    # -- Workers (run in background thread) ----------------------------------

    def _local_reply_worker(self, text: str) -> None:
        try:
            context  = "\n".join(self.bus.context_log.snapshot())
            prompt   = f"Recent terminal activity:\n{context}\n\nUser: {text}"
            response = self._local_convo.send_with_full_context(prompt).strip()
            self.bus.call_from_thread(
                self.ui_write,
                f"[bold green]LLM:[/] {response}",
                False,
            )
        except Exception as e:
            self.bus.call_from_thread(
                self.ui_write,
                f"[red][LLM ERROR][/red] {e}",
                False,
            )

    def _mesh_reply_worker(self, packet: dict) -> None:
        sender = packet.get("fromId", "unknown")
        dest   = packet.get("fromId")

        try:
            convo    = self._get_node_convo(sender)
            prompt   = f"Received packet addressed to you: {packet}"
            response = truncate_for_mesh(convo.send_with_full_context(prompt).strip())

            if self.auto_reply and dest:
                self.iface.sendText(response, destinationId=dest, wantAck=True)
                self.bus.call_from_thread(
                    self.ui_write,
                    f"[bold green][LLM SENT -> {dest}][/bold green] {response}",
                )
            else:
                self.bus.call_from_thread(
                    self.ui_write,
                    f"[yellow][LLM DRY RUN -> {dest}][/yellow] {response}",
                )

        except Exception as e:
            self.bus.call_from_thread(
                self.ui_write,
                f"[red][LLM ERROR][/red] {e}",
            )

    # -- Helpers -------------------------------------------------------------

    def _get_node_convo(self, node_id: str):
        if node_id not in self._node_convos:
            self._node_convos[node_id] = LLMConversation(
                base_url=self.base_url,
                model=self.model,
                system_prompt=_PACKET_PROMPT,
            )
        return self._node_convos[node_id]

    def _should_reply(self, packet: dict, raw_msg: str) -> bool:
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
