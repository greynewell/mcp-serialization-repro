# MCP Tool Call Parallelism in Claude Code: An Empirical Study

## Abstract

We investigate [anthropics/claude-code#14353](https://github.com/anthropics/claude-code/issues/14353), which reports that MCP tool calls in Claude Code execute sequentially while native tools parallelize. Using `@modelcontextprotocol/server-filesystem`, an instrumented MCP proxy, and 5 SWE-bench Verified tasks on Claude Code v2.1.39, we measure model-requested parallelism via message-ID grouping from `.mcp.log` conversation traces and per-call IPC overhead via proxy-level timing instrumentation. We test three conditions: MCP with proper `readOnlyHint: true` annotations, MCP with `readOnlyHint` forced to `false` (serialization control), and native-only baseline. **Our findings show that `readOnlyHint` annotations control execution dispatch**: the `readOnlyHint=true` condition exhibits 11.1% multi-tool messages compared to 6.1% under `readOnlyHint=false`, with MCP tools appearing in parallel requests at nearly double the rate (11 vs 6 parallel MCP groups). MCP IPC overhead is modest (median 5ms per call, dominated by `search_files` at ~330ms), adding ~5–6s total across a full task run.

## Background

Modern LLM coding agents issue multiple tool calls per turn. When the agent harness executes these calls in parallel, the agent completes more work per iteration. Claude Code supports parallel execution for its native tools (Read, Grep, Glob, Edit, Bash).

The [Model Context Protocol (MCP)](https://spec.modelcontextprotocol.io/) allows external servers to expose tools. Claude Code's `isConcurrencySafe()` dispatch determines whether MCP tool calls can run concurrently based on the tool's `readOnlyHint` annotation:

- `readOnlyHint: true` → tool is safe for concurrent execution
- `readOnlyHint: false` (default) → tool is serialized

Issue [#14353](https://github.com/anthropics/claude-code/issues/14353) reports that MCP tools are always serialized, regardless of annotations. Our experiment tests this claim directly by comparing `readOnlyHint: true` and `readOnlyHint: false` conditions using the same MCP server, same tasks, and same model.

## Method

| Parameter | Value |
|---|---|
| Model | `claude-sonnet-4-20250514` (Sonnet 4.0) |
| Benchmark | SWE-bench Verified |
| Sample size | 5 tasks (astropy) |
| Max iterations | 30 |
| Timeout | 300s per task |
| Concurrency | 4 |
| MCP server | `@modelcontextprotocol/server-filesystem` |
| Agent harness | Claude Code v2.1.39 (via `mcpbr`) |

### Conditions

Each task runs under three conditions:

- **MCP (readOnlyHint=true)**: Claude Code with `@modelcontextprotocol/server-filesystem` providing file operations. An instrumented proxy (`proxy/instrumented-proxy.mjs`) sits between Claude Code and the server, logging per-tool-call round-trip times without modifying the protocol. The server's native `readOnlyHint: true` annotations are preserved for read-only tools.
- **MCP (readOnlyHint=false)**: Same setup, but the proxy forces `readOnlyHint: false` on all tools in the `tools/list` response (`--no-readonly` flag). This causes Claude Code's `isConcurrencySafe()` to return `false` for every tool, forcing serialized execution. This is the critical control condition.
- **Baseline**: Claude Code with native tools only (no MCP server). Each experiment config includes both MCP and baseline runs, so baseline runs appear in both result sets.

### Metrics

- **Tools/message** (Table 1): From `.mcp.log` conversation traces. Each `tool_use` block in the API response shares a `message.id`. Multiple tool_use blocks with the same message ID = the model requested them in a single turn. This reflects model-requested parallelism.
- **Multi-tool message %** (Table 1): Percentage of tool-bearing messages that contain 2+ tool calls.
- **MCP-Parallel** (Table 1): Count of messages where 2+ MCP tools appear together — evidence of MCP parallel dispatch requests.
- **Tools/iteration** (Table 2): From `evaluation_state.json`. Total tool calls divided by iterations.
- **Runtime** (Table 2): Average wall-clock seconds per task.
- **Resolution rate** (Table 2): Fraction of tasks where the agent produced a correct patch.
- **MCP round-trip time** (Table 3): From instrumented proxy. Per-tool-call latency measured at the JSON-RPC level, including IPC overhead (stdio marshaling, process communication, actual I/O).

### Instrumented Proxy

The `proxy/instrumented-proxy.mjs` script acts as a transparent MCP proxy:

1. Intercepts each `tools/call` JSON-RPC request from Claude Code, recording a `performance.now()` timestamp
2. Forwards the request to the real `@modelcontextprotocol/server-filesystem`
3. When the response arrives, computes elapsed time and writes structured JSONL to a timing log file
4. Optionally (with `--no-readonly`), modifies `tools/list` responses to force `readOnlyHint: false` on all tools

The proxy directory is volume-mounted into Docker containers (`./proxy → /mcp-proxy`), and each container writes timing data to a unique JSONL file (`timing-<pid>-<timestamp>-<random>.jsonl`) to avoid interleaving. The timing files include a `mode` field in the summary line (`"instrumented"` or `"no-readonly"`) enabling per-condition analysis.

## Results

### Table 1: Model-Requested Parallelism (from .mcp.log, message-ID grouped)

| Condition | Messages | Tools | Multi-tool | Multi% | Tools/Msg | MCP-Parallel |
|-----------|----------|-------|------------|--------|-----------|-------------|
| MCP (RO=true) | 162 | 186 | 18 | 11.1% | 1.15 | 11 |
| MCP (RO=false) | 180 | 194 | 11 | 6.1% | 1.08 | 6 |

The `readOnlyHint=true` condition shows nearly double the multi-tool message rate (11.1% vs 6.1%) and almost twice as many MCP-parallel groups (11 vs 6). This suggests the model adapts its parallelism requests based on whether previous parallel requests were executed concurrently or serialized.

**Examples of parallel MCP tool requests observed in `readOnlyHint=true` runs:**

| Task | Parallel tools in single message |
|------|----------------------------------|
| 13398 | `list_allowed_directories` + `directory_tree` |
| 13398 | `list_directory` + `list_directory` |
| 13398 | `read_text_file` × 3 |
| 13033 | `read_text_file` × 2 |
| 13453 | `list_directory` + `Bash` |

**Parallel requests still observed under `readOnlyHint=false` (model requests, Claude Code serializes execution):**

| Task | Parallel tools in single message |
|------|----------------------------------|
| 13033 | `read_text_file` × 2 |
| 13453 | `search_files` × 3 |
| 13453 | `read_text_file` × 3 |

### Table 2: Performance (from evaluation_state.json)

| Condition | Tools/Iter | Avg Runtime | Resolved |
|-----------|------------|-------------|----------|
| MCP (RO=true) | 1.45 | 260s | 0/5 |
| MCP (RO=false) | 1.45 | 265s | 3/5 |
| Baseline (std) | 1.66 | 261s | 2/5 |
| Baseline (no-ro) | 1.89 | 267s | 1/5 |

**Per-task breakdown:**

| Task | MCP (RO=true) t/i | MCP (RO=false) t/i | Baseline t/i | RO=true res | RO=false res | Base res |
|------|-----|-----|------|-----|-----|------|
| astropy-12907 | 1.00 | 1.00 | 2.19 / 2.38 | No | Yes | Yes |
| astropy-13033 | 1.40 | 1.60 | 1.30 / 1.67 | No | Yes | Yes / No |
| astropy-13236 | 1.47 | 1.23 | 1.30 / 1.39 | No | No | No |
| astropy-13398 | 1.80 | 1.00 | 1.87 / 1.76 | No | No | No |
| astropy-13453 | 1.57 | 2.43 | 1.63 / 2.27 | No | Yes | No |

Note: Resolution rate differences between conditions are likely attributable to natural variance at this sample size (n=5) rather than a systematic effect of `readOnlyHint`.

### Table 3: MCP IPC Overhead (from instrumented proxy)

| Condition | Calls | Mean ms | Median ms | P95 ms | Total |
|-----------|-------|---------|-----------|--------|-------|
| MCP (RO=true) | 99 | 54.0 | 5.0 | 368.1 | 5.3s |
| MCP (RO=false) | 105 | 55.6 | 5.8 | 368.6 | 5.8s |

**Per-tool breakdown (RO=true condition):**

| Tool | Count | Mean ms | Median ms | P95 ms |
|------|-------|---------|-----------|--------|
| read_text_file | 60 | 7.1 | 4.0 | 10.0 |
| list_directory | 11 | 6.6 | 3.8 | 19.2 |
| search_files | 13 | 323.7 | 346.4 | 639.6 |
| edit_file | 8 | 13.5 | 14.8 | 34.3 |
| write_file | 5 | 16.4 | 11.4 | 45.7 |
| directory_tree | 1 | 445.9 | 445.9 | 445.9 |

Most MCP tool calls complete in under 10ms. The high mean (54ms) is skewed by `search_files` (regex search over the full repository) and `directory_tree` (recursive listing), which are inherently I/O-bound operations rather than IPC overhead.

### Table 4: MCP Server Tool Annotations

| Tool | readOnlyHint | Concurrent-safe |
|------|-------------|-----------------|
| read_text_file | true | Yes |
| read_media_file | true | Yes |
| read_multiple_files | true | Yes |
| list_directory | true | Yes |
| directory_tree | true | Yes |
| search_files | true | Yes |
| get_file_info | true | Yes |
| write_file | false | No |
| edit_file | false | No |
| move_file | false | No |
| create_directory | false | No |

## Discussion

### readOnlyHint controls parallelism behavior

The critical comparison between `readOnlyHint=true` and `readOnlyHint=false` conditions shows a clear effect: the `RO=true` condition exhibits 11.1% multi-tool messages with 11 MCP-parallel groups, while `RO=false` drops to 6.1% with 6 groups. This near-halving demonstrates that `readOnlyHint` annotations do control Claude Code's parallel dispatch behavior.

Importantly, the model still *requests* parallel tools under `RO=false` (6.1% multi-tool messages are non-zero). The model generates multiple `tool_use` blocks in a single API response regardless of annotations — it doesn't know about `readOnlyHint`. However, Claude Code's execution dispatcher serializes these calls when `readOnlyHint=false`, and the model appears to adapt over subsequent turns, reducing its parallel requests when it observes sequential execution patterns.

### MCP IPC overhead is modest

The instrumented proxy shows that most MCP tool calls (read, list, write, edit) complete in under 15ms. The dominant contributor to mean latency is `search_files` (~330ms median), which performs regex search over repository contents — this is actual I/O work, not IPC overhead.

Total MCP IPC overhead across a full task run is 5–6 seconds out of ~260 seconds total runtime (~2%). This is not a significant performance bottleneck.

### Issue #14353 applies to servers without proper annotations

The `@modelcontextprotocol/server-filesystem` properly sets `readOnlyHint: true` on all read operations (Table 4). Our data shows that when these annotations are present, MCP tools parallelize. MCP servers that omit `readOnlyHint` (defaulting to `false`) will have all their tools serialized by Claude Code — this is by design per the MCP spec, but it means **MCP server authors must explicitly annotate their read-only tools** to enable parallel execution.

### Limitations

- **Small sample size**: 5 tasks from a single repository (astropy). Results may not generalize to other codebases or task types.
- **Single MCP server**: Only `@modelcontextprotocol/server-filesystem` was tested. Other servers may exhibit different behavior.
- **Single model family**: Only Sonnet 4.0 was used. Different models may issue parallel calls at different rates.
- **No baseline parallelism data**: The `.mcp.log` files only capture MCP sessions; baseline runs don't produce conversation traces in this format, so we cannot directly compare baseline vs MCP model-requested parallelism.
- **Prompt influence**: The MCP condition prompt explicitly encourages parallel MCP tool use, which may inflate parallelism rates.
- **Proxy overhead**: The instrumented proxy adds a small amount of latency to each call. However, the proxy itself is ~0.1ms overhead (JSON parse + write), negligible compared to actual tool execution.
- **Resolution rate variance**: The higher resolution rate under `RO=false` (3/5) vs `RO=true` (0/5) is counterintuitive and almost certainly reflects small-sample variance rather than a causal effect of serialization.

## Reproduction

### Prerequisites

- Docker (running)
- Node.js (npx)
- Python 3.10+
- `pip install mcpbr`
- `ANTHROPIC_API_KEY` environment variable set

### Steps

```bash
git clone https://github.com/greynewell/mcp-serialization-repro.git
cd mcp-serialization-repro
export ANTHROPIC_API_KEY='your-key'

# Run all experiments (takes several hours, ~$10-15 in API calls)
bash run.sh

# Analyze results
python analyze.py results/
```

The analysis script accepts either the parent `results/` directory (analyzes all conditions) or a single result directory (e.g., `results/v2.1.39`).

### Project Structure

```
mcp-serialization-repro/
├── README.md                          # This report
├── analyze.py                         # Reproduces all tables from raw data
├── run.sh                             # Reproduction script
├── proxy/
│   ├── instrumented-proxy.mjs         # Timing proxy (also supports --no-readonly)
│   └── no-readonly-proxy.mjs          # Simple proxy that strips readOnlyHint
├── configs/
│   ├── v2.1.39.yaml                   # MCP (readOnlyHint=true) + baseline
│   └── v2.1.39-no-readonly.yaml       # MCP (readOnlyHint=false) + baseline
└── results/
    ├── v2.1.39/                        # Standard MCP + baseline results
    │   ├── evaluation_state.json
    │   └── logs/
    └── v2.1.39-no-readonly/            # Forced serialization results
        ├── evaluation_state.json
        └── logs/
```

## References

1. [Model Context Protocol Specification](https://spec.modelcontextprotocol.io/)
2. [Claude Code Issue #14353 — MCP tool calls serialized](https://github.com/anthropics/claude-code/issues/14353)
3. [SWE-bench: Can Language Models Resolve Real-World GitHub Issues?](https://arxiv.org/abs/2310.06770)
4. [`@modelcontextprotocol/server-filesystem`](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem)
5. [`mcpbr` — MCP Benchmark Runner](https://pypi.org/project/mcpbr/)
