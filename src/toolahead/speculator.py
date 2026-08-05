"""Transition memory and lightweight intent parsing for ToolAhead."""

import json
import os
import re


class Context:
    def __init__(self):
        self.last_grep_hits: list[str] = []


# ------------------------------------------------- Transition-Table (Markov)

class TransitionTable:
    """Markov-Übergangsstatistik über kanonisierte Tool-Events, inkl.
    $START-Zustand, Confusion-Tracker (Negativ-Cache) und Persistenz."""

    def __init__(self):
        self.counts: dict[tuple[str, str], float] = {}
        self.wrong: dict[tuple[str, str], float] = {}
        # Kanonisierte Transitionen duerfen absichtlich grober als ein echter
        # Tool-Call sein (z. B. ``bash:test:unittest``). Fuer eine Spekulation
        # brauchen wir trotzdem konkrete Argumente. Deshalb merken wir pro
        # kanonischem Ziel die beobachteten, exakten Beispiele und verwenden
        # das haeufigste. Der Ergebnis-Cache prueft spaeter weiterhin den
        # exakten Call; ein unpassendes Beispiel kann also nur ein Miss sein.
        self.examples: dict[str, dict[str, float]] = {}

    def record(self, prev: str, nxt: str, example: dict | None = None):
        self.counts[(prev, nxt)] = self.counts.get((prev, nxt), 0) + 1
        if example is not None:
            encoded = json.dumps(example, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
            bucket = self.examples.setdefault(nxt, {})
            bucket[encoded] = bucket.get(encoded, 0) + 1

    def example(self, key: str) -> dict | None:
        bucket = self.examples.get(key, {})
        if not bucket:
            return None
        encoded = max(bucket.items(), key=lambda item: item[1])[0]
        try:
            value = json.loads(encoded)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def record_miss(self, prev: str, predicted: str):
        self.wrong[(prev, predicted)] = self.wrong.get((prev, predicted), 0) + 1

    def predict(self, prev: str) -> tuple[str | None, float]:
        cands = [(n, c) for (p, n), c in self.counts.items() if p == prev]
        if not cands:
            return None, 0.0
        total = sum(c for _, c in cands)
        nxt, c = max(cands, key=lambda x: x[1])
        if self.wrong.get((prev, nxt), 0) >= 3:  # Confusion-Tracker: unterdrückt
            return None, 0.0
        return nxt, c / total

    def decay(self, factor: float = 0.8):
        self.counts = {k: v * factor for k, v in self.counts.items() if v * factor >= 0.1}
        self.wrong = {k: v * factor for k, v in self.wrong.items() if v * factor >= 0.1}
        self.examples = {
            key: {example: count * factor for example, count in bucket.items()
                  if count * factor >= 0.1}
            for key, bucket in self.examples.items()
        }
        self.examples = {key: bucket for key, bucket in self.examples.items() if bucket}

    def save(self, path: str):
        data = {"counts": [[p, n, c] for (p, n), c in self.counts.items()],
                "wrong": [[p, n, c] for (p, n), c in self.wrong.items()],
                "examples": [[key, example, count]
                             for key, bucket in self.examples.items()
                             for example, count in bucket.items()]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path: str) -> "TransitionTable":
        t = cls()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            t.counts = {(p, n): c for p, n, c in data.get("counts", [])}
            t.wrong = {(p, n): c for p, n, c in data.get("wrong", [])}
            t.examples = {}
            for key, example, count in data.get("examples", []):
                t.examples.setdefault(key, {})[example] = count
        return t


# --------------------------------------------------- Intent-Parser (Stufe 0)

# Intent-Parsing prefetcht nur, was aus dem Thinking eindeutig auflösbar ist —
# also konkrete Datei-Reads. Bash/Test-Kommandos sind im Thinking NICHT als
# exaktes Kommando genannt; die übernimmt die Transition-Table (Stufe 1),
# die das reale Kommando aus dem Stream gelernt hat.
INTENT_PATTERNS = [
    (re.compile(r"(lese|öffne|read|open|look at|view|inspect).{0,60}?([\w./-]+\.(py|ts|js|go|rs))",
                re.I | re.S),
     lambda m: f"read:{m.group(2)}"),
    (re.compile(r"([\w./-]+\.(py|ts|js|go|rs)).{0,40}(lese|öffne|anschauen|read|open|inspect)",
                re.I | re.S),
     lambda m: f"read:{m.group(1)}"),
]


def parse_intents(text: str) -> list[str]:
    found = []
    for pat, build in INTENT_PATTERNS:
        m = pat.search(text)
        if m:
            key = build(m)
            if key not in found:
                found.append(key)
    return found
