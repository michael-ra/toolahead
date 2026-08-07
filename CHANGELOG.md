# Changelog

## Unreleased

- Added optional service pre-warming via `toolahead.toml`: declared dev
  servers and other long-lived prerequisites start after the first successful
  edit (or at daemon start) and are health-checked via port, HTTP, or command
  probes before the real call runs.
- Added a workspace-trust gate (`toolahead trust`): service commands from a
  repository config never execute until the exact file content is approved
  once; the SHA-256 approval lives outside the repository (mode 0600) and any
  change to `toolahead.toml` revokes it automatically.
- Declared external commands are now an execution opt-in for the MCP `run`
  tool, separate from the replay allowlist: they execute after a blocking,
  verified readiness check (actionable error when a required service stays
  down) but are never replayed or cached.
- Added a zero-latency freshness note: when a run starts within seconds of an
  edit against an already-running service, the output flags that a hot-reload
  server may still serve the previous build.
- Hardened the service lifecycle: deduplicated background ensure threads, a
  closed flag so nothing restarts after shutdown, rejection of non-finite
  timeouts, and per-service crash-loop disable after three failed launches.
- Separated the two acceleration paths explicitly: commands declared under
  `[commands]` with `requires` are external-state commands — they are never
  run ahead and never served from cache; only their services are pre-warmed.
- Added `/__prefetch/ensure-services` and a bounded, fail-open readiness wait
  in the MCP server before real command execution (`TOOLAHEAD_ENSURE_WAIT`,
  `0` disables; no-op without `toolahead.toml`).
- Service processes run in their own process groups, log to
  `.toolahead/services/`, and stop with the daemon.
- Excluded `.toolahead/` runtime state from workspace hashing, watching, and
  sandbox copies.
- Added a unit test suite for service configuration, matching, lifecycle, and
  engine integration, wired into CI.

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
