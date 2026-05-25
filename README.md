# vmesh

vmesh is a terminal app for talking on a Meshtastic mesh network.

It gives you a live chat-style interface, supports broadcast and direct messages, and has a small plugin system so you can add features without touching the core app.

## What it does

- Connects to a Meshtastic device over serial (`--port`) or TCP (`--host`)
- Shows incoming messages in a clean terminal UI
- Lets you send:
  - broadcast messages with `/send`
  - direct messages with `/sendto`
- Tracks delivery acknowledgements for direct messages
- Shows node and local device info (`/nodes`, `/info`)
- Auto-syncs device timezone on connect

## What's neat about it

- Fast feedback loop: open terminal, connect, chat
- Plugin-friendly design: load features with `--feature path/to/file.py`
- Lightweight event bus for feature hooks (`on_packet`, `on_connect`, `on_send`)
- Optional local LLM feature for helper chat and mesh auto-reply

## Requirements

- Python 3.10+
- A Meshtastic device (USB serial or network reachable)

## Setup

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install meshtastic textual tzlocal PyPubSub
```

Optional (for the LLM feature):

```bash
pip install voltllmclient
```

## Running

Serial device:

```bash
python vmesh.py --port /dev/ttyUSB0
```

TCP device:

```bash
python vmesh.py --host 192.168.1.100
```

With a feature plugin:

```bash
python vmesh.py --port /dev/ttyUSB0 --feature features/llm.py
```

You can pass `--feature` more than once.

## In-app commands

- `/send your message`
- `/sendto "Node Name" your message`
- `/nodes`
- `/info`
- `/help`
- `/quit`

If the node name has spaces, keep it in quotes.

## Notes

- Direct messages are truncated to mesh-safe length.
- The app tries to set the device timezone to your local timezone on connect.
- For the LLM feature, make sure your local model endpoint is available (default: `http://localhost:11434`).
