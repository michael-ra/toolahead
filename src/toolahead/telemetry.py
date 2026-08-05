"""Provider-neutrale Turn- und Tool-Latenztelemetrie fuer ToolAhead.

Die Uhr laeuft im lokalen Daemon. Hook- und Stream-Adapter schicken nur
Ereignisse; Prompt-/Tool-Inhalte werden hier bewusst nicht gespeichert.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _distribution(values: deque[float]) -> dict[str, float | int | None]:
    points = list(values)
    if not points:
        return {"count": 0, "mean_ms": None, "p50_ms": None,
                "p95_ms": None, "total_ms": 0.0}
    return {
        "count": len(points),
        "mean_ms": round(sum(points) / len(points), 2),
        "p50_ms": round(_percentile(points, 0.50) or 0.0, 2),
        "p95_ms": round(_percentile(points, 0.95) or 0.0, 2),
        "total_ms": round(sum(points), 2),
    }


class LatencyTracker:
    """Korreliert Codex/Claude-Hooks, Spekulationen und Replays.

    ``agent_wait_ms`` ist absichtlich nicht ``api_ms``: zwischen zwei lokalen
    Hook-Ereignissen liegen Netzwerk, Provider-Queue, Inferenz und Reasoning.
    Ohne Provider-Transport-Trace lassen sich diese Anteile nicht ehrlich
    weiter zerlegen.
    """

    METRICS = (
        "agent_wait_ms",
        "prompt_to_first_reasoning_ms",
        "reasoning_window_ms",
        "tool_phase_ms",
        "hook_lookup_ms",
        "prefetch_lead_ms",
        "replay_wait_ms",
        "measured_tool_saved_ms",
        "turn_wall_ms",
    )

    def __init__(self, max_points: int = 4096, max_recent: int = 100):
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: dict[tuple[str, str], dict[str, Any]] = {}
        self.metrics = {name: deque(maxlen=max_points) for name in self.METRICS}
        self.recent: deque[dict[str, Any]] = deque(maxlen=max_recent)
        self.reasoning_events = 0
        self.summary_events = 0
        self.raw_reasoning_events = 0
        self.commentary_events = 0

    @staticmethod
    def _session_id(event: dict) -> str:
        return str(event.get("session_id") or event.get("thread_id") or "unknown")

    @staticmethod
    def _turn_id(event: dict) -> str | None:
        value = event.get("turn_id")
        return str(value) if value is not None else None

    @staticmethod
    def _tool_id(event: dict) -> str:
        value = event.get("tool_use_id") or event.get("item_id")
        if value is not None:
            return str(value)
        return f"anonymous:{time.monotonic_ns()}"

    def _session(self, event: dict, now: float) -> dict[str, Any]:
        sid = self._session_id(event)
        return self.sessions.setdefault(sid, {
            "session_id": sid,
            "turn_id": self._turn_id(event),
            "prompt_at": None,
            "anchor_at": now,
            "anchor_kind": "session",
            "first_reasoning_at": None,
            "last_reasoning_at": None,
            "turn_started_at": None,
            "turn_closed": False,
        })

    def _append(self, metric: str, value_ms: float | None):
        if value_ms is not None and math.isfinite(value_ms):
            self.metrics[metric].append(max(0.0, value_ms))

    def record_hook(self, event: dict) -> dict[str, Any]:
        now = time.monotonic()
        name = str(event.get("hook_event_name") or event.get("event") or "")
        with self.lock:
            state = self._session(event, now)
            if self._turn_id(event):
                state["turn_id"] = self._turn_id(event)

            if name == "SessionStart":
                state.update({"anchor_at": now, "anchor_kind": "session"})
                return {"recorded": name}

            if name == "UserPromptSubmit":
                state.update({
                    "prompt_at": now,
                    "turn_started_at": now,
                    "anchor_at": now,
                    "anchor_kind": "prompt",
                    "first_reasoning_at": None,
                    "last_reasoning_at": None,
                    "turn_closed": False,
                })
                return {"recorded": name}

            if name == "PreToolUse":
                call_id = self._tool_id(event)
                call = {
                    "session_id": state["session_id"],
                    "turn_id": self._turn_id(event) or state.get("turn_id"),
                    "tool_use_id": call_id,
                    "tool": event.get("tool_name") or event.get("tool") or "unknown",
                    "label": event.get("label") or "unknown",
                    "pre_at": now,
                    "post_at": None,
                    "agent_wait_ms": None,
                    "reasoning_window_ms": None,
                    "tool_phase_ms": None,
                    "hook_lookup_ms": None,
                    "prefetch_lead_ms": None,
                    "replay_wait_ms": None,
                    "native_spec_ms": None,
                    "measured_tool_saved_ms": None,
                    "hit": False,
                    "reserved": False,
                    "source": event.get("source", "hook"),
                }
                anchor = state.get("anchor_at")
                if anchor is not None:
                    call["agent_wait_ms"] = (now - anchor) * 1000
                    self._append("agent_wait_ms", call["agent_wait_ms"])
                first = state.get("first_reasoning_at")
                if first is not None:
                    call["reasoning_window_ms"] = (now - first) * 1000
                    self._append("reasoning_window_ms", call["reasoning_window_ms"])
                self.calls[(state["session_id"], call_id)] = call
                return {"recorded": name, "tool_use_id": call_id}

            if name == "PostToolUse":
                call_id = self._tool_id(event)
                call = self.calls.get((state["session_id"], call_id))
                if call is None:
                    # PostToolUse kann nach Resume oder bei spaet zugestelltem
                    # unified-exec-Result ohne lokales Pre-Ereignis eintreffen.
                    call = {
                        "session_id": state["session_id"],
                        "turn_id": self._turn_id(event) or state.get("turn_id"),
                        "tool_use_id": call_id,
                        "tool": event.get("tool_name") or "unknown",
                        "label": event.get("label") or "unknown",
                        "pre_at": None,
                        "hit": False,
                        "source": event.get("source", "hook"),
                    }
                    self.calls[(state["session_id"], call_id)] = call
                call["post_at"] = now
                if call.get("pre_at") is not None:
                    actual = (now - call["pre_at"]) * 1000
                    call["tool_phase_ms"] = actual
                    self._append("tool_phase_ms", actual)
                    native = call.get("native_spec_ms")
                    if native is not None and call.get("hit"):
                        measured = native - actual
                        call["measured_tool_saved_ms"] = measured
                        # Negative values are meaningful per-call, but the
                        # latency distribution represents hidden time only.
                        self._append("measured_tool_saved_ms", max(0.0, measured))
                state.update({"anchor_at": now, "anchor_kind": "tool",
                              "first_reasoning_at": None,
                              "last_reasoning_at": None})
                self.recent.append(self._public_call(call))
                return {"recorded": name, "tool_use_id": call_id}

            if name in ("Stop", "SessionEnd"):
                started = state.get("turn_started_at")
                if started is not None and not state.get("turn_closed"):
                    wall = (now - started) * 1000
                    self._append("turn_wall_ms", wall)
                    state["last_turn_wall_ms"] = wall
                    state["turn_closed"] = True
                return {"recorded": name}

            return {"recorded": name or "unknown"}

    def record_reasoning(self, event: dict):
        now = time.monotonic()
        kind = str(event.get("stream_kind") or "summary")
        with self.lock:
            state = self._session(event, now)
            if state.get("first_reasoning_at") is None:
                state["first_reasoning_at"] = now
                prompt = state.get("prompt_at")
                if prompt is not None:
                    self._append("prompt_to_first_reasoning_ms", (now - prompt) * 1000)
            state["last_reasoning_at"] = now
            self.reasoning_events += 1
            if kind == "raw":
                self.raw_reasoning_events += 1
            elif kind == "commentary":
                self.commentary_events += 1
            else:
                self.summary_events += 1

    def record_lookup(self, event: dict, *, elapsed_s: float, hit: bool,
                      entry: dict | None = None, info: dict | None = None):
        sid = self._session_id(event)
        call_id = str(event.get("tool_use_id") or "")
        if not call_id:
            return
        with self.lock:
            call = self.calls.get((sid, call_id))
            if call is None:
                return
            elapsed_ms = elapsed_s * 1000
            call["hook_lookup_ms"] = elapsed_ms
            call["reserved"] = bool(hit)
            self._append("hook_lookup_ms", elapsed_ms)
            source = entry or info or {}
            scheduled = source.get("scheduled_at") or source.get("created")
            if scheduled is not None and call.get("pre_at") is not None:
                lead = (call["pre_at"] - scheduled) * 1000
                call["prefetch_lead_ms"] = max(0.0, lead)
                self._append("prefetch_lead_ms", max(0.0, lead))
            duration = source.get("dur")
            if duration is not None:
                call["native_spec_ms"] = duration * 1000

    def record_replay(self, event: dict, *, entry: dict, wait_s: float):
        sid = self._session_id(event)
        call_id = str(event.get("tool_use_id") or "")
        if not call_id:
            return
        with self.lock:
            call = self.calls.get((sid, call_id))
            if call is None:
                return
            call["hit"] = True
            call["native_spec_ms"] = entry.get("dur", 0.0) * 1000
            call["replay_wait_ms"] = wait_s * 1000
            self._append("replay_wait_ms", wait_s * 1000)

    @staticmethod
    def _public_call(call: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "session_id", "turn_id", "tool_use_id", "tool", "label", "source",
            "hit", "reserved", "agent_wait_ms", "reasoning_window_ms", "tool_phase_ms",
            "hook_lookup_ms", "prefetch_lead_ms", "replay_wait_ms",
            "native_spec_ms", "measured_tool_saved_ms",
        )
        result = {key: call.get(key) for key in fields}
        for key, value in list(result.items()):
            if key.endswith("_ms") and isinstance(value, (int, float)):
                result[key] = round(value, 2)
        return result

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "semantics": {
                    "agent_wait_ms": "local hook gap: network + queue + inference + reasoning",
                    "tool_phase_ms": "PreToolUse to PostToolUse wall time",
                    "measured_tool_saved_ms": "speculative native runtime minus actual replay tool phase",
                    "turn_wall_ms": "UserPromptSubmit to Stop; not a paired baseline speedup",
                },
                "reasoning": {
                    "events": self.reasoning_events,
                    "summary_events": self.summary_events,
                    "raw_events": self.raw_reasoning_events,
                    "commentary_events": self.commentary_events,
                },
                "distributions": {
                    name: _distribution(values) for name, values in self.metrics.items()
                },
                "recent_tools": list(self.recent),
            }
