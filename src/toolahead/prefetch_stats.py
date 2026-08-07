#!/usr/bin/env python3
"""Status-Dashboard des Prefetch-Proxys — für die Kommandozeile / Claude Code.

Fragt GET /__prefetch/stats ab und rendert eine kompakte Übersicht:
gesparte Wall-Clock-Zeit, verworfene Spekulations-CPU, Trefferquote,
Aufschlüsselung pro Tool und die gelernte Transition-Table.

    python3 prefetch_stats.py [--url http://127.0.0.1:4242] [--json]

Als Claude-Code-Slash-Command via .claude/commands/prefetch.md (siehe dort).
"""

import argparse
import json
import sys
import urllib.request


def bar(frac: float, width: int = 24) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def ms(value) -> str:
    return "   —" if value is None else f"{value:7.1f}ms"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4242")
    ap.add_argument("--json", action="store_true", help="Rohdaten als JSON")
    args = ap.parse_args()

    try:
        with urllib.request.urlopen(args.url.rstrip("/") + "/__prefetch/stats",
                                    timeout=5) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"✗ Prefetch-Proxy nicht erreichbar unter {args.url} ({e})")
        print("  Läuft der Proxy? Start: python3 proxy.py")
        sys.exit(1)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    s = data["stats"]
    cfg = data.get("config", {})
    wat = data.get("watcher", {})
    saved = s.get("net_saved_s", 0.0)
    wasted = s.get("wasted_s", 0.0)
    acc = s.get("acceptance_rate")
    eff = s.get("efficiency")
    delivery = s.get("delivery_rate")
    latency = data.get("latency", {})
    distributions = latency.get("distributions", {})

    print("╭─ Spekulativer Tool-Prefetcher " + "─" * 34)
    print(f"│  Workspace : {cfg.get('workspace', '?')}")
    print(f"│  Sandbox   : {'an' if cfg.get('sandbox') else 'aus'}   "
          f"Bash-Konfidenz: {cfg.get('bash_conf')}   Budget: {cfg.get('max_expensive')}"
          f"   Lookup-Limit: {cfg.get('lookup_wait_s', '?')}s")
    print(f"│  Watcher   : {wat.get('backend', '?')}  (gen {wat.get('generation', 0)}, "
          "nur Optimierung)")
    print(f"│  Mutationen: Generation {cfg.get('mutation_generation', 0)} · "
          f"{s.get('mutations', 0)} beobachtet · "
          f"{cfg.get('mutation_debounce_ms', 0)}ms Debounce")
    print(f"│  🔑 SHA-256 : {s.get('serve_hashes', 0)}× beim Serving + "
          f"{s.get('background_hashes', 0)}× im Hintergrund")
    print("├─ Wirkung " + "─" * 55)
    print(f"│  ⏱  Wall-Clock gespart   : {saved:6.2f}s")
    print(f"│  🗑  Spekulations-CPU weg : {wasted:6.2f}s  "
          f"(verworfen: {s.get('invalidated', 0)})")
    if eff is not None:
        print(f"│  ⚖  Effizienz (gespart/[gespart+weg]) : {bar(eff)} {eff * 100:4.0f}%")
    if acc is not None:
        print(f"│  🎯 Trefferquote (hits/lookups)       : {bar(acc)} {acc * 100:4.0f}%  "
              f"({s.get('hits', 0)}✓ / {s.get('misses', 0)}✗)")
    if delivery is not None:
        print(f"│  📦 Replay ausgeliefert               : {bar(delivery)} {delivery * 100:4.0f}%  "
              f"({s.get('replays', 0)}/{s.get('reservations', 0)})")
    print(f"│  Spekulationen: {s.get('scheduled', 0)} gestartet · "
          f"{s.get('sandbox_runs', 0)} Bash-Sandbox · {s.get('gated', 0)} vom EV-Gate gebremst")
    if s.get("superseded_runs") or s.get("mutation_restarts") \
            or s.get("mutation_coalesced"):
        print(f"│  ↻ Latest wins: {s.get('superseded_runs', 0)} abgebrochen "
              f"({s.get('superseded_s', 0):.2f}s) · "
              f"{s.get('mutation_restarts', 0)} neu gestartet · "
              f"{s.get('mutation_coalesced', 0)} gebündelt")
    if s.get("lookup_timeouts") or s.get("client_aborts"):
        print(f"│  Fallbacks: {s.get('lookup_timeouts', 0)} Lookup-Timeouts · "
              f"{s.get('client_aborts', 0)} Client-Abbrüche")

    if any(item.get("count") for item in distributions.values()):
        print("├─ Latenz (lokal gemessene Wall Clock) " + "─" * 27)
        print(f"│  {'Phase':31}{'p50':>10}{'p95':>10}{'n':>6}")
        latency_rows = (
            ("Agent-Wartezeit*", "agent_wait_ms"),
            ("Prompt → erstes Reasoning", "prompt_to_first_reasoning_ms"),
            ("Reasoning-Fenster → Tool", "reasoning_window_ms"),
            ("Toolphase Pre → Post", "tool_phase_ms"),
            ("Hook + exakter Lookup", "hook_lookup_ms"),
            ("Prefetch-Vorlauf", "prefetch_lead_ms"),
            ("Replay-Restwartezeit", "replay_wait_ms"),
            ("Toolphase wirklich gespart", "measured_tool_saved_ms"),
            ("Turn gesamt (kein A/B)", "turn_wall_ms"),
        )
        for label, key in latency_rows:
            item = distributions.get(key, {})
            if item.get("count"):
                print(f"│  {label:31}{ms(item.get('p50_ms')):>10}"
                      f"{ms(item.get('p95_ms')):>10}{item.get('count', 0):>6}")
        reasoning = latency.get("reasoning", {})
        if reasoning.get("events"):
            print(f"│  Reasoning-Events: {reasoning.get('summary_events', 0)} Summary · "
                  f"{reasoning.get('raw_events', 0)} Raw · "
                  f"{reasoning.get('commentary_events', 0)} Commentary")
        print("│  * Netzwerk + Queue + Inferenz + Reasoning; nicht reine API-Netzzeit")

        recent = latency.get("recent_tools", [])
        if recent:
            print("├─ Letzte Tool-Timelines " + "─" * 39)
            for item in recent[-5:]:
                state = "HIT" if item.get("hit") else \
                    "RESV" if item.get("reserved") else "MISS"
                print(f"│  {str(item.get('label', '?'))[:23]:23} {state:4} · "
                      f"wait {ms(item.get('agent_wait_ms')).strip()} · "
                      f"tool {ms(item.get('tool_phase_ms')).strip()} · "
                      f"lead {ms(item.get('prefetch_lead_ms')).strip()} · "
                      f"saved {ms(item.get('measured_tool_saved_ms')).strip()}")

    services = data.get("services", {})
    if services:
        print("├─ Services (Pre-Warming) " + "─" * 39)
        if cfg.get("services_trusted") is False:
            print("│  ⚠ toolahead.toml nicht freigegeben — Services starten "
                  "erst nach `toolahead trust`")
        icons = {"ready": "✓", "starting": "…", "stopped": "·",
                 "exited": "✗", "disabled": "⊘", "untrusted": "⚠"}
        for name, info in sorted(services.items()):
            state = str(info.get("state", "?"))
            pid = info.get("pid")
            check = info.get("ready_check") or "process"
            detail = f"pid {pid}" if pid else \
                f"{info.get('failures', 0)} Fehlstart(s)" \
                if info.get("failures") else "aus"
            print(f"│  {icons.get(state, '?')} {name:20} {state:9} · "
                  f"prewarm={info.get('prewarm', '?'):8} · "
                  f"ready={check:7} · {detail}")
        if s.get("external_diverted"):
            print(f"│  ≋ {s['external_diverted']}× externes Kommando → "
                  "Pre-Warming statt Spekulation")

    per = data.get("per_tool", {})
    if per:
        print("├─ Pro Tool " + "─" * 54)
        print(f"│  {'Tool':7}{'geplant':>9}{'serviert':>10}{'gespart':>10}"
              f"{'CPU weg':>10}")
        for tool, d in sorted(per.items(), key=lambda x: -x[1].get("saved_s", 0)):
            print(f"│  {tool:7}{d.get('scheduled', 0):>9}{d.get('served', 0):>10}"
                  f"{d.get('saved_s', 0):>9.2f}s{d.get('wasted_s', 0):>9.2f}s")

    table = data.get("table", [])
    if table:
        print("├─ Gelernte Übergänge (Top) " + "─" * 38)
        for prev, nxt, c in table[:8]:
            print(f"│  {prev:22} → {nxt:22} ×{c:g}")
    print("╰" + "─" * 64)


if __name__ == "__main__":
    main()
