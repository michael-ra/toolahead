<div align="center">
  <img alt="ToolAhead" src="https://raw.githubusercontent.com/michael-ra/toolahead/main/docs/assets/toolahead-hero.svg" width="240">

  <h3>Your agent's next tool call, already done.</h3>

  <p>
    <strong>28% faster agents</strong> — median of paired runs.<br>
    Finish faster. Wait less.
  </p>

  <p><code>uvx toolahead</code></p>

  <p>
    ToolAhead learns recurring tool sequences in a repository and starts safe,
    repeatable calls before Codex or Claude Code requests them. Prepared output
    is returned only when the eventual call and workspace match exactly.
  </p>

  <p>
    <img alt="CI status" src="https://github.com/michael-ra/toolahead/actions/workflows/ci.yml/badge.svg">
    <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&labelColor=111827">
    <img alt="Claude Code" src="https://img.shields.io/badge/Claude%20Code-supported-FFCC00?style=flat-square&labelColor=111827">
    <img alt="Codex CLI" src="https://img.shields.io/badge/Codex%20CLI-supported-FFCC00?style=flat-square&labelColor=111827">
    <img alt="Antigravity" src="https://img.shields.io/badge/Antigravity-supported-FFCC00?style=flat-square&labelColor=111827">
    <img alt="MCP" src="https://img.shields.io/badge/MCP-native-8b5cf6?style=flat-square&labelColor=111827">
    <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-22c55e?style=flat-square&labelColor=111827">
  </p>
</div>

## Stop waiting for tools

Agents normally work serially:

```text
reason → call tool → wait → inspect → reason → call tool → wait
```

ToolAhead learns which calls usually follow each other. It starts the likely
next call while the model is still working:

```text
Agent       inspect result ───── reason ───── request next tool ── result
ToolAhead                  └──── run predicted tool ──────────────┘
```

The agent still calls ordinary MCP tools. If no matching result is ready, the
tool runs normally. If ToolAhead prepared the exact call against the exact same
files, the result returns immediately from memory.

ToolAhead also hides a second kind of waiting: declared dev servers and other
slow prerequisites start right after the first edit — while the model is still
reasoning — so they are already warm and health-checked when the test or e2e
call arrives. In a real Next.js session that turned a 3.5s dev-server wait
into 0.45s. See
[Pre-warming external services](#pre-warming-external-services-optional).

## See it run

### Codex: the same task with and without ToolAhead

<p align="center">
  <img src="https://raw.githubusercontent.com/michael-ra/toolahead/main/docs/assets/toolahead-codex-speedup.gif"
       alt="Codex CLI baseline versus ToolAhead synchronized real-run speed comparison"
       width="100%">
</p>

This is a 1× timeline from a matched Codex pair using the real API and separate
copies of the same project. The protocol and paired Codex/Claude measurements
are in [BENCHMARKS.md](BENCHMARKS.md).

### Full recorded runs

These 1× recordings show ToolAhead handling the complete workflow: list,
search, read, edit, write, test, result validation, and reuse.

#### Codex CLI

<p align="center">
  <img src="https://raw.githubusercontent.com/michael-ra/toolahead/main/docs/assets/toolahead-codex-live.gif"
       alt="Real Codex CLI run using ToolAhead list, search, read, edit, and run tools"
       width="100%">
</p>

#### Claude Code

<p align="center">
  <img src="https://raw.githubusercontent.com/michael-ra/toolahead/main/docs/assets/toolahead-claude-live.gif"
       alt="Real Claude Code run using ToolAhead list, search, read, edit, and run tools"
       width="100%">
</p>

## Install

Once published on PyPI:

```bash
uvx toolahead --help
# or
python3 -m pip install toolahead
```

From a local checkout today:

```bash
git clone https://github.com/michael-ra/toolahead.git
cd toolahead
uvx --from . toolahead --help
```

Requirements: Python 3.11+, macOS or Linux, and an authenticated Codex CLI or
Claude Code installation. `watchdog` is optional.

## Quickstart

Run these commands inside the project you want to accelerate:

```bash
# Connect both agents to ToolAhead and install the required hooks.
uvx toolahead init --agent both --strict --project .
# Google Antigravity user? Add --agent all, or run: uvx toolahead init-antigravity
# (writes the workspace-local .agents/mcp_config.json Antigravity discovers)

# Allow this exact test command to run ahead and be reused.
uvx toolahead allow "python3 -m pytest" --project .

# Optional: declare your dev server in toolahead.toml, then approve it once
# so it can be pre-warmed while the model reasons.
uvx toolahead trust --project .

# Start ToolAhead in the background for this workspace.
uvx toolahead serve --workspace .
```

Then start your agent in a second terminal.

Codex CLI:

```bash
codex
```

Claude Code:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4242 claude
```

See live timing and cache statistics at any time:

```bash
uvx toolahead status
```

Rerun `toolahead init` after upgrading ToolAhead. It refreshes ToolAhead's
project files without changing unrelated Codex, Claude, or MCP settings.

## One clear set of tools

ToolAhead gives the agent one consistent set of MCP tools. This lets it return
prepared results directly instead of waiting for a native tool to run and then
trying to replace its result afterward.

| MCP tool | Familiar input | Can run ahead | Behavior |
| --- | --- | :---: | --- |
| `list_files` | `pattern`, `path`, `limit` | ✓ | Lists matching files |
| `search` | `pattern`, `path`, `glob`, output mode | ✓ | Searches file contents |
| `read_file` | `file_path`, `offset`, `limit` | ✓ | Reads a file with line numbers |
| `edit_file` | `file_path`, `old_string`, `new_string`, `replace_all` | — | Makes an exact edit and starts the next prediction |
| `write_file` | `file_path`, `content` | — | Creates or replaces a file and starts the next prediction |
| `run` | `command`, `description` | ✓ | Runs approved tests, builds, and linters |

The agent never sees cache wrappers or duplicate JSON. ToolAhead keeps cache
timing in hidden MCP `_meta`; prepared and normal calls return the same text,
errors, and exit codes.

### Why `--strict` matters

Showing two equivalent Read tools forces the model to choose between duplicate
options, wastes prompt space, and makes selection less reliable. Strict mode
keeps one set:

- Claude Code's project settings hide native `Read`, `Grep`, `Glob`, `Edit`,
  and `Write`; the six ToolAhead MCP equivalents take their place.
- Codex sees the same six tools and instructions to use them. Strict mode
  redirects native `apply_patch` to `edit_file` so
  edit→test learning stays intact. Codex's general shell remains available when
  needed; explicitly allowed Bash tests can still reuse prepared results.
- Tool names and field conventions stay close to the native coding-agent tools.
  Descriptions are intentionally short to reduce the tokens sent to the model.

Omit `--strict` if you want to keep all native file tools visible while trying
ToolAhead.

## Predictions can be wrong. Returned results cannot.

ToolAhead is free to guess what comes next, but it returns prepared work only
when the requested call and current files are exact matches.

```mermaid
flowchart LR
    A[Previous tool or turn start] --> B[Predict next exact call]
    B --> C[Read-only worker or disposable checkout]
    A --> D[Agent keeps reasoning]
    C --> E{Exact call + fresh SHA-256 input match?}
    D --> E
    E -->|match| F[Return prepared result from RAM]
    E -->|no match| G[Execute the MCP call normally]
```

- List, Search, and Read results are tied to the exact request and the relevant
  file contents.
- Command results are tied to the exact command and a fresh hash of the whole
  workspace.
- Commands run ahead only in a disposable workspace copy.
- A prepared result is returned only when the real workspace still matches the
  copy used to create it.
- Wrong predictions, background-process failures, expired results, and
  timeouts automatically fall back to a normal tool execution.
- Cache entries store stdout, stderr, and exit code—not a model-generated
  summary.

ToolAhead learns tool sequences locally. The reliable signal is the previous
tool finishing; visible commentary can offer an earlier hint when an agent
provides it. Private chain-of-thought is never required.

### Latest file change wins

ToolAhead does not need to guess which edit will be the last one. Every
successful Edit or Write increases a simple workspace version number:

```text
edit version 1 ── start predicted tests
edit version 2 ── stop version 1 ── restart tests on version 2
edit version 3 ── stop version 2 ── keep only the version 3 result
```

- A running command for an older file version receives `SIGTERM` as a process
  group, then `SIGKILL` if it does not stop promptly.
- The pending command is restarted for the newest file version even when
  another edit arrives before the test request.
- Writes arriving within 50 ms are grouped before work starts. Configure the
  window with `PREFETCH_MUTATION_DEBOUNCE_MS`; set it to `0` to disable
  grouping.
- Outdated results are never inserted into the current cache. Fresh SHA-256
  validation remains the final replay condition.
- Failed file changes do not increase the workspace version.

In plain terms: after every successful file change, ToolAhead starts the likely
next safe call. Nearby changes are grouped, and a newer change always replaces
work started for an older file state.

## Which commands can be reused

ToolAhead may return a prepared command result instead of running the command
again only when that exact command is listed in `.prefetch-replay.json`:

```json
{
  "commands": [
    "python3 -m pytest",
    "npm test"
  ]
}
```

Use the CLI instead of editing the file by hand:

```bash
toolahead allow "python3 -m pytest" --project .
```

The allowed-command list updates without restarting ToolAhead. It rejects shell
chains, pipes, redirects, substitutions, installers, and arbitrary commands;
recognized test/lint families include unittest, pytest, npm/yarn tests, Go,
Cargo, Make, Jest, Vitest, Ruff, ESLint, TypeScript, and mypy.

### Prioritize known failures without weakening the result

Use the test runner's explicit full-suite mode when available. For pytest,
[`pytest --ff`](https://docs.pytest.org/en/latest/reference/reference.html#cmdoption-ff)
runs the last failures first and then the rest of the suite;
ToolAhead can learn and reuse that exact command normally. Focused modes such
as `pytest --lf` or Jest `--onlyFailures` are useful quick checks, but ToolAhead
never substitutes their partial result for a requested full-suite result.

## Pre-warming external services (optional)

ToolAhead accelerates two different things and never mixes them up:

- **Result speculation** prepares an answer ahead of time. It is limited to
  calls whose output is a pure function of the workspace files — List, Search,
  Read, and allowlisted test commands — because the content hash proves the
  result is identical.
- **Pre-warming** starts slow, long-lived prerequisites ahead of time — a dev
  server, a browser. No result is ever served from memory here; the win is
  purely the eliminated startup latency.

Commands whose output depends on a running service (Playwright against a dev
server, integration tests against a database) belong to the second category: a
file hash cannot prove their results equal, because server state — hot-reload
timing included — is not stored in files. Declare them in an optional
`toolahead.toml` at the workspace root:

```toml
[services.dev-server]
command = "npm run dev"
ready.port = 3000        # or ready.http = "http://…" / ready.command = "curl -sf …"
timeout = 30             # seconds to wait for readiness (default 30)
prewarm = "mutation"     # "mutation" (default) | "start" | "manual"

[commands.e2e]
match = "npx playwright test"   # prefix match on the exact command
requires = ["dev-server"]
```

You can also let your agent draft this file — it already knows the project's
dev-server command, ports, and test entry points, and writing the file is an
ordinary edit. Nothing executes from it until you review and approve the exact
content once:

```bash
toolahead trust
```

Service commands run unsandboxed against the live workspace — they *are* the
environment the agent is about to test — so a cloned repository must never
start anything by merely being opened. `toolahead trust` records a SHA-256 of
the exact file outside the repository (mode 0600); **any** later change to
`toolahead.toml` revokes the approval automatically until you rerun it. Until
trusted, only the safe direction applies: declared external commands are still
excluded from result reuse, but no process is ever started.

To keep a service itself sandboxed, make the declared command the sandbox
wrapper: `command = "docker compose up dev"` runs the dev server in a
container with the workspace mounted read-only or read-write as you choose —
isolation comes from the container, and `ready.port` works unchanged.

With a trusted config:

- Services with `prewarm = "mutation"` start right after the first successful
  edit — typically while the model is still reasoning about its next step — so
  they are warm when the test or e2e call arrives. `"start"` launches them with
  the daemon, `"manual"` only on demand.
- A command matching a `[commands.*]` entry is **never** run ahead and never
  served from cache, but it *is* allowed through the ToolAhead `run` tool
  (execution opt-in, separate from the replay allowlist). Before executing,
  the `run` tool waits — bounded by the declared timeouts — until every
  required service passes its readiness check, and returns an actionable error
  instead of a doomed run when a service stays down. Runs through the agent's
  native shell get best-effort pre-warming only; the readiness guarantee
  applies to the ToolAhead `run` tool.
- Readiness means *reachable*, not "has processed your latest edit": a
  hot-reload server that was already running may briefly still serve the
  previous build. ToolAhead never adds a wait for this — instead, when a run
  starts within seconds of an edit against an already-running service, it
  appends a short note to the output so the agent re-runs once instead of
  concluding its change had no effect. For a strict freshness barrier, use
  `ready.command` with a project-specific check (for example comparing a build
  ID endpoint against the sources).
- Browser-based checks follow the same rule: ToolAhead warms the browser and
  the server, but a screenshot or page snapshot is always captured fresh —
  rendered output is not a pure function of the files.

Everything here is strictly opt-in: without `toolahead.toml` nothing starts and
nothing changes. `TOOLAHEAD_ENSURE_WAIT=0` disables the bounded wait before
real commands entirely. Service output is logged to
`.toolahead/services/<name>.log`, and `toolahead status` shows each service's
state.

## Latency metrics

`toolahead status` separates the parts that can otherwise be confused:

| Metric | Meaning |
| --- | --- |
| Agent wait | Time from the previous result until the agent asks for its next tool; includes API, network, model, and reasoning time |
| Prefetch lead | How long ToolAhead had already been running the call before the agent asked for it |
| Replay wait | How much longer the prepared call still needed when the agent requested it |
| Tool wait removed | Native tool runtime minus actual replay/tool phase |
| End-to-end | Total time for the complete task; includes variable agent and API time |
| Acceptance | Prepared calls that exactly matched and were returned |
| Delivery | Prepared command results the agent actually requested and used |

This is why removing 5 seconds of tool waiting does not guarantee the complete
task finishes exactly 5 seconds sooner: model and API response times vary
independently.

## Security model

> [!WARNING]
> A disposable workspace copy is not a security sandbox. Allow only commands
> you already trust. A malicious command can still access the network or write
> to absolute paths outside the copy.

- Tool paths are contained inside the configured workspace; symlink escapes are
  rejected.
- Every command run ahead uses a fresh disposable copy, never the live
  checkout.
- The local daemon binds to `127.0.0.1` and adds no remote telemetry.
- Declared services never start from an untrusted `toolahead.toml`: approval
  is an explicit `toolahead trust` of the exact file content, stored outside
  the repository and revoked automatically by any change to the file.
- Before returning a prepared result, ToolAhead hashes the current files again.
  Filesystem watchers only help it skip unnecessary hashing.
- Tests that depend on external services, databases, clocks, random values, or
  environment state cannot be validated from source files alone. Declare them
  under `[commands]` in `toolahead.toml`: they are then excluded from result
  reuse and only their prerequisites are pre-warmed.

## Limitations

- Edit and Write are intentionally not run ahead. After either finishes,
  ToolAhead starts the next predicted safe tool. Rapid changes are grouped, and
  commands running against an older file state are stopped.
- Prepared command results are limited to explicitly approved tests, builds,
  and linters whose output should be repeatable.
- Commands currently verify the entire workspace, which can be conservative on
  very large monorepos. Checking only relevant dependencies is planned.
- API and model response times can outweigh the saved tool time. Compare
  multiple runs with and without ToolAhead instead of relying on one attempt.
- Hosted tools such as provider-side web search cannot be run ahead by this
  local integration.
- Service readiness proves reachability, not that a hot-reload server has
  finished rebuilding the newest edit. ToolAhead flags this window with a note
  on the run output rather than adding latency; `ready.command` can implement
  a strict project-specific freshness check.
- Windows has not yet been validated.

## Development

Build and verify the PyPI artifacts:

```bash
uv build
python3 .github/scripts/normalize_sdist.py dist/*.tar.gz
python3 .github/scripts/check_distribution.py dist/*.whl dist/*.tar.gz
uvx --from twine twine check dist/toolahead-0.2.0a2*
uvx --from dist/toolahead-0.2.0a2-py3-none-any.whl toolahead --help
```

## Project map

- `src/toolahead/` — installable CLI, MCP server, prediction engine, hooks,
  sandbox execution, replay, and telemetry
- `docs/assets/` — the logo and README recordings
- `.github/workflows/` — package validation and trusted PyPI publishing
- `.github/scripts/` — release-archive privacy and metadata checks

## Research foundations

ToolAhead is an independent implementation informed by research on speculative
tool execution. It is not an official implementation or reproduction of any
single paper. The closest foundations are:

- [SPORK: Self-Speculative Forking to Accelerate Agentic LLM Inference](https://arxiv.org/abs/2607.03333)
  and [Parallelizing Tool Execution and LLM Generation (PASTE)](https://arxiv.org/abs/2603.18897)
  for overlapping predicted tool execution with ongoing agent reasoning.
- [Speculate with Memory](https://arxiv.org/abs/2607.12236) for learning
  recurring action transitions from previous agent trajectories.
- [SpecBox](https://arxiv.org/abs/2607.23933) for speculative sandbox
  prewarming and isolated execution.
- [AOSpec](https://arxiv.org/abs/2608.00881) for lossless action/state
  verification across speculative execution.

ToolAhead combines these directions with local Codex and Claude Code hooks,
exact call-and-workspace matching, MCP result replay, mutation generations, and
a standalone Python package. All benchmark numbers above are ToolAhead's own
measurements, not results reported by those papers.

## License

MIT License. See [LICENSE](LICENSE).

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Security
reports should follow [SECURITY.md](SECURITY.md).
