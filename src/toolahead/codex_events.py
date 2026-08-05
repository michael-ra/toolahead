#!/usr/bin/env python3
"""Relay Codex JSONL while forwarding reasoning signals to ToolAhead.

Supported inputs:

* ``codex exec --json`` events (stable non-interactive interface)
* Codex app-server JSON-RPC notifications, including streamed summary/raw
  reasoning deltas (the app-server protocol is currently experimental)

Examples::

    python3 codex_events.py -- codex exec --json "fix the tests"
    codex exec --json "inspect the repo" | python3 codex_events.py

Every input line is emitted unchanged. Local event delivery is asynchronous
and fail-open, so a missing ToolAhead daemon cannot delay Codex materially.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import urllib.request
from typing import Any, Iterable


DEFAULT_URL = os.environ.get(
    "TOOLAHEAD_EVENT_URL",
    os.environ.get("TOOLAHEAD_URL", "http://127.0.0.1:4242").rstrip("/")
    + "/__prefetch/agent-event")


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_texts(item))
        return found
    if isinstance(value, dict):
        for key in ("text", "value", "content", "summary"):
            if key in value:
                return _texts(value[key])
    return []


class CodexEventParser:
    def __init__(self):
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.delta_seen: set[tuple[str, str]] = set()

    def parse(self, message: dict) -> list[dict]:
        """Return normalized ToolAhead Reasoning events for one wire event."""
        if not isinstance(message, dict):
            return []
        event_type = str(message.get("type") or "")
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if event_type == "thread.started":
            self.thread_id = str(message.get("thread_id") or self.thread_id or "unknown")
        elif method == "thread/started":
            thread = params.get("thread") if isinstance(params.get("thread"), dict) else params
            self.thread_id = str(thread.get("id") or params.get("threadId")
                                 or self.thread_id or "unknown")
        if event_type == "turn.started":
            self.turn_id = str(message.get("turn_id") or self.turn_id or "unknown")
        elif method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
            self.turn_id = str(turn.get("id") or params.get("turnId")
                               or self.turn_id or "unknown")

        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            text = params.get("delta") or params.get("text") or ""
            if not isinstance(text, str) or not text:
                return []
            kind = "raw" if method.endswith("/textDelta") else "summary"
            item_id = str(params.get("itemId") or "unknown")
            self.delta_seen.add((item_id, kind))
            return [self._event(text, kind, params)]

        item = message.get("item") if isinstance(message.get("item"), dict) else None
        if item and item.get("type") in ("agent_message", "agentMessage"):
            # ``codex exec --json`` may omit reasoning items even when usage
            # reports reasoning tokens. Visible interim agent commentary is
            # still a useful, non-hidden intent signal.
            text = item.get("text")
            phase = item.get("phase")
            if isinstance(text, str) and text and phase in (None, "commentary"):
                return [self._event(text, "commentary", message)]
        if item and item.get("type") in ("reasoning", "reasoning_item"):
            item_id = str(item.get("id") or "unknown")
            events: list[dict] = []
            # ``codex exec --json`` may expose only completed reasoning items;
            # app-server clients normally receive the deltas above first.
            if (item_id, "summary") not in self.delta_seen:
                for text in _texts(item.get("summary")):
                    events.append(self._event(text, "summary", message))
            if (item_id, "raw") not in self.delta_seen:
                for text in _texts(item.get("content")):
                    events.append(self._event(text, "raw", message))
            return events
        return []

    def _event(self, text: str, kind: str, source: dict) -> dict:
        params = source.get("params") if isinstance(source.get("params"), dict) else {}
        return {
            "event": "Reasoning",
            "session_id": str(params.get("threadId") or source.get("thread_id")
                              or self.thread_id or "unknown"),
            "turn_id": str(params.get("turnId") or source.get("turn_id")
                           or self.turn_id or "unknown"),
            "stream_kind": kind,
            "text": text,
            "source": "codex-event-stream",
        }


class AsyncSender:
    def __init__(self, url: str, timeout: float = 0.5):
        self.url = url
        self.timeout = timeout
        self.queue: queue.Queue[dict | None] = queue.Queue(maxsize=512)
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="toolahead-codex-events")
        self.thread.start()

    def submit(self, event: dict):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # Intent hints are opportunistic; dropping is safer than applying
            # backpressure to the agent stream.
            pass

    def _run(self):
        while True:
            event = self.queue.get()
            if event is None:
                return
            try:
                request = urllib.request.Request(
                    self.url, method="POST", data=json.dumps(event).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response.read()
            except Exception:
                pass

    def close(self):
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            return
        self.thread.join(timeout=2)


def relay(lines: Iterable[str], sender: AsyncSender, output) -> None:
    parser = CodexEventParser()
    for line in lines:
        output.write(line)
        output.flush()
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            continue
        for event in parser.parse(message):
            sender.submit(event)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="optional command after --, usually codex exec --json ...")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    sender = AsyncSender(args.url, args.timeout)
    try:
        if not command:
            relay(sys.stdin, sender, sys.stdout)
            return 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True,
                                   bufsize=1)
        assert process.stdout is not None
        relay(process.stdout, sender, sys.stdout)
        return int(process.wait())
    finally:
        sender.close()


if __name__ == "__main__":
    raise SystemExit(main())
