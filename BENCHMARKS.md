# ToolAhead benchmark

ToolAhead `0.2.0a2` was measured with five matched baseline/warm pairs on
Codex CLI and five on Claude Code: 20 authenticated real-agent runs in total.

## Results

| Agent | Correct runs | Warm acceleration | Paired wins | Median E2E change | Median tool wait removed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Codex CLI · `gpt-5.6-sol` | 10/10 | 5/5 | **5/5** | **19.926 s faster · 28.7%** | 9.44 s |
| Claude Code · Fable 5 | 10/10 | 5/5 | 2/5 | 1.087 s slower · −2.4% | **10.34 s** |

Paired baseline-minus-ToolAhead wall-clock differences:

- Codex: `+22.668, +17.935, +19.926, +2.733, +26.168` seconds.
- Claude: `+6.230, −1.087, −7.855, +37.976, −8.254` seconds.

The bootstrap 95% range for the paired median was `[2.733, 26.168]` seconds
for Codex and `[−8.254, 37.976]` seconds for Claude. Five pairs are useful
product evidence, not a universal performance guarantee.

## Protocol

- Codex CLI `0.146.0`; Claude Code `2.1.197`.
- Real authenticated provider APIs and models; no mock model.
- Fresh isolated workspaces and identical MCP contracts for every run.
- Alternating baseline→warm and warm→baseline order inside matched pairs.
- Twelve tool calls per task: list, search, four reads, failing tests, one
  rejected edit, three successful mutations, and final tests.
- The controlled test command took approximately 7.5 seconds.
- Every run remained in the result, including slower ToolAhead runs and one
  correct Codex run that performed an additional rejected edit.

Each warm run delivered eight exact cache hits and two test-result replays.
All 20 tasks produced the expected files and passed an independent final test.
Claude consistently removed tool waiting, but variable provider/model time was
larger than that saving in three of its five end-to-end pairs.
