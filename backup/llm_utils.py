"""
llm_utils.py — LLM conversation management for vmesh
"""

from datetime import datetime
from voltllmclient import LLMConversation

MAX_MESSAGE_LEN = 180

LLM_MODEL    = "gemma3:4b"
LLM_BASE_URL = "http://localhost:11434"

BOT_NAME = "@sigyl"

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


class LLMManager:
    """
    Manages LLM conversations: one per remote node (for auto-reply),
    plus a single persistent local assistant conversation.
    """

    def __init__(self, model: str, base_url: str):
        self.model    = model
        self.base_url = base_url

        self._node_conversations: dict[str, LLMConversation] = {}

        self.local = LLMConversation(
            base_url=self.base_url,
            model=self.model,
            system_prompt=local_chat_system_prompt,
        )
        self.local.client.temperature = 0

    def get_node_conversation(self, node_id: str) -> LLMConversation:
        """Return (creating if needed) the conversation for a remote node."""
        if node_id not in self._node_conversations:
            self._node_conversations[node_id] = LLMConversation(
                base_url=self.base_url,
                model=self.model,
                system_prompt=packet_response_prompt,
            )
        return self._node_conversations[node_id]

    def close_all(self) -> None:
        """Save all node conversations to timestamped JSON files."""
        date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
        for node_id, convo in self._node_conversations.items():
            safe_id = node_id if node_id is not None else "unknown_sender"
            for ch in [':', '/', '\\', '*', '?', '"', '<', '>', '|']:
                safe_id = safe_id.replace(ch, '_')
            filename = f"{date_str}_{safe_id}.json"
            convo.save_transcript(filename)
