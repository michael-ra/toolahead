# Changelog

## 0.6.1 — 2026-08-08

Fixes from an adversarial review of 0.6.0. Every item below was reproduced
before it was fixed and now has a regression test.

- Re-running `toolahead init` without `--strict` after a strict install left
  Claude's native file tools denied and Codex's strict marker in place while
  removing the MCP tools that replaced them — the documented upgrade path
  could leave an agent with no file tools at all. Routing now always matches
  the flags of the current run.
- Route learning only accepts URLs a command actually fetched with an HTTP
  client (directly or inside an executed shell script). A URL that was merely
  printed, commented, or contained in a file the agent read is no longer
  learned, so it can no longer become an unrequested GET after a later edit.
- Learned routes are session-memory only and are never persisted, so a cloned
  repository cannot ship a `.prefetch-table.json` that steers ToolAhead's
  automatic requests past the trust gate. `toolahead trust` now prints the
  auto-GET targets it is approving.
- `--url` reaches the installed hooks, and readiness requests carry their
  workspace; a daemon serving a different workspace now refuses them instead
  of answering for the wrong project.
- Hook process timeouts, the client readiness budget, and the server-side wait
  are now consistent, so the readiness check can no longer be cut short by the
  hook being killed mid-wait.
- Warm requests for a service never overlap: a newer edit waits for the
  in-flight round instead of racing it.
- `derive_routes` resolves paths against the workspace, so absolute paths work
  and paths outside the workspace produce no route.
- Antigravity `PreToolUse` returns the required `{"decision": "allow"}` neutral
  response instead of an empty object, `Stop` is installed as a flat handler
  list, and the working directory of the call is preferred over the first
  workspace path.
- `toolahead doctor` no longer reports a correct hooks-only installation as
  broken.
- The Claude hook uses `http.client` instead of `urllib`, cutting about 19 ms
  of interpreter startup from every hooked tool call (measured 60 ms → 41 ms).

A second adversarial pass over those fixes found and corrected the following,
including one regression introduced by the first round:

- The strict-routing rollback deleted `permissions.deny` entries it had never
  written — a project that denies `Edit`/`Write` as its own policy lost that
  rule on the first plain `toolahead init`. ToolAhead now records which
  entries it added and removes only those.
- The learned transition table moved out of the repository (into
  `~/.toolahead/tables/`). A cloned repository could otherwise ship a
  `.prefetch-table.json` that made ToolAhead speculatively execute commands
  before anything was trusted. Set `PREFETCH_TABLE` to override the location.
- The workspace check now also covers `/__prefetch/agent-event` and
  `/__prefetch/lookup`, not just readiness: those endpoints start services,
  drive speculation, and hand out file contents. A subdirectory of the project
  still counts as the same workspace.
- Route learning is quoting-aware and multi-line aware, ignores commands that
  failed, and again recognises scripts run as `./e2e.sh`, `. ./e2e.sh` or
  `time ./e2e.sh`. A URL inside a quoted string can no longer masquerade as a
  fetch.
- `[commands]` rules match a declared script however the agent invokes it
  (`sh e2e.sh`, `bash ./e2e.sh`, `./e2e.sh`), so the readiness guarantee no
  longer depends on the exact spelling the model happens to choose.
- `warm_routes = ["auto"]` derives routes relative to the service's `cwd`, so
  it works for a monorepo service rooted in a subdirectory.
- Removing the managed MCP block from `.codex/config.toml` no longer truncates
  everything after a missing end marker, and it removes duplicated blocks.
- A small `TOOLAHEAD_ENSURE_WAIT` is no longer collapsed to a one-second
  server budget, which produced false "service not ready" denials.
- The MCP server sends its workspace and wait budget like the hooks do.

## 0.6.0 — 2026-08-07

- Hooks-only is the new default: `toolahead init` now replaces nothing — the
  agent keeps its native tools, and learning, service pre-warming, route
  warming, and transparent Bash replay all ride on lifecycle hooks. The
  ToolAhead MCP tools (Read/Search replay hits) became an explicit opt-in via
  `--replay-tools`; `--strict` implies it. Re-running `init` without the flag
  removes a previous registration.
- Claude Code hooks now report native tool events (PostToolUse,
  UserPromptSubmit, Stop), so mutations and learning no longer depend on the
  MCP tools or the API proxy.
- The readiness guarantee for declared external commands moved into the
  PreToolUse hooks of all three agents: before a matching native shell
  command runs, the hook waits — bounded by the declared timeouts — for the
  required services, and denies with an actionable reason only when a
  trusted config's service stays down. Everything else stays fail-open, and
  `TOOLAHEAD_ENSURE_WAIT=0` disables the wait.

## 0.5.0 — 2026-08-07

- Added Antigravity lifecycle hooks: `init-antigravity` now also installs
  `.agents/hooks.json` plus a fail-open adapter that reports Antigravity's
  native tool events (`run_command`, `view_file`, `replace_file_content`, …)
  to the daemon. Learning, mutation-triggered service pre-warming, and route
  warming now work even when the agent uses only native tools; the ToolAhead
  MCP tools remain necessary for replay hits.
- The adapter always answers with a neutral `{}` decision and exit code 0 —
  a malformed hook response could otherwise block the agent's tool calls.

## 0.4.1 — 2026-08-07

- Route warming now also learns: URLs the agent actually requests after
  editing a file — extracted from its commands and the workspace shell
  scripts they reference, for declared service origins only — are recorded
  as file→route transitions and warmed via `"auto"` on the next edit of that
  file. Learned transitions live in the existing transition table with the
  same persistence and decay.

## 0.4.0 — 2026-08-07

- Added route warming: `warm_routes = ["/", "auto"]` on a service GETs the
  listed routes after every edit once the service is ready, so on-demand
  compiling dev servers (Next.js and friends) finish their rebuild before the
  agent's browser or e2e check arrives. `"auto"` derives the route from the
  edited file for Next.js (app and pages router), Nuxt, and SvelteKit.
- Warm requests are GET-only against the declared service origin, never
  follow redirects off it, never cache responses, and a newer edit
  supersedes an in-flight warm round. Manual services are never booted just
  for warming.

## 0.3.2 — 2026-08-07

- Added Google Antigravity support: `toolahead init-antigravity` writes the
  workspace-local `.agents/mcp_config.json` that the Antigravity CLI and IDE
  discover, and `toolahead init --agent all` covers Codex, Claude Code, and
  Antigravity together. Learning and latest-mutation-wins are driven by the
  MCP server's own lifecycle reports — no native hooks required.

## 0.3.1 — 2026-08-07

- Fixed README images on PyPI by using absolute asset URLs, and added the
  one-line install to the top of the README.

## 0.3.0 — 2026-08-07

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
