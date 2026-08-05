# Changelog

## 0.2.0a2 — 2026-08-05

- Prefetch the complete learned safe chain at turn start, including List,
  Search, Read, and allowlisted commands.
- Hardened latest-mutation-wins ordering, stale-timer promotion, cancellation,
  and exact replay revalidation after reservation.
- Added native Codex Bash replay through lifecycle hooks and compatibility for
  Codex's `query` search argument.
- Fixed workspace hashing so ignored runtime, Git, cache, and dependency trees
  are actually pruned instead of being eagerly traversed.
- Added a 20-run real Codex/Claude crossover matrix with separate task,
  contract, safety, acceleration, tool-overlap, provider, and wall metrics.
- Added 46 integration and robustness tests covering batched chains,
  multi-edit races, runtime hash isolation, and reservation invalidation.

## 0.2.0a1 — 2026-08-05

- Added a six-tool MCP surface for Codex CLI and Claude Code.
- Added exact in-memory replay for List, Search, Read, and opted-in commands.
- Added strict routing, project-local installers, and live status metrics.
- Moved speculative commands into disposable workspace copies.
- Added fresh content validation, filesystem watching, and replay safety gates.
- Added latest-mutation-wins scheduling: mutation generations, process-group
  cancellation, automatic latest-state restarts, and configurable write
  coalescing.
- Added matched real-agent benchmarks and recorded Codex/Claude terminal demos.
