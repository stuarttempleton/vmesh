"""
context_log.py — Log of ongoing context (for plugins, etc...)
"""

class ContextLog:
    def __init__(self, max_entries: int = 50):
        self._entries: list[str] = []
        self.max_entries = max_entries

    def append(self, text: str) -> None:
        self._entries.append(text)
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)

    def snapshot(self) -> list[str]:
        return list(self._entries)