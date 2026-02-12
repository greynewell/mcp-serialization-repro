# MCP Tool Call Parallelism in Claude Code

> **Finding:** MCP tools *do* parallelize in Claude Code — when the server sets `readOnlyHint: true`. Servers that omit this annotation (default `false`) get serialized. This is by design, not a bug. See [#14353](https://github.com/anthropics/claude-code/issues/14353).

## Key Results

**Parallelism** — `readOnlyHint=true` shows ~2× the parallel dispatch rate of `readOnlyHint=false`:

| Condition | Multi-tool msgs | Tools/Msg | MCP-Parallel groups |
|-----------|:-:|:-:|:-:|
| MCP (`readOnlyHint=true`) | 11.1% | 1.15 | 11 |
| MCP (`readOnlyHint=false`) | 6.1% | 1.08 | 6 |

**IPC overhead** — MCP adds ~2% wall-clock time per task (median 5ms/call):

| Tool | Count | Median ms | P95 ms |
|------|:-:|:-:|:-:|
| read_text_file | 60 | 4.0 | 10.0 |
| list_directory | 11 | 3.8 | 19.2 |
| search_files | 13 | 346.4 | 639.6 |
| edit_file | 8 | 14.8 | 34.3 |
| write_file | 5 | 11.4 | 45.7 |

**Performance** — No meaningful difference between conditions at this sample size:

| Condition | Tools/Iter | Avg Runtime | Resolved |
|-----------|:-:|:-:|:-:|
| MCP (`RO=true`) | 1.45 | 260s | 0/5 |
| MCP (`RO=false`) | 1.45 | 265s | 3/5 |
| Baseline (native) | 1.66–1.89 | 261–267s | 1–2/5 |

Resolution rate variance is noise at n=5.

## What This Means for MCP Server Authors

Claude Code's `isConcurrencySafe()` checks `readOnlyHint` to decide parallel vs serial execution. The `@modelcontextprotocol/server-filesystem` correctly annotates its tools:

| Read-only (`readOnlyHint: true`) | Mutating (`readOnlyHint: false`) |
|---|---|
| read_text_file, read_media_file, read_multiple_files, list_directory, directory_tree, search_files, get_file_info | write_file, edit_file, move_file, create_directory |

**If your MCP server's tools aren't parallelizing, add `readOnlyHint: true` to your read-only tool annotations.**

## Method

Tested on Claude Code v2.1.39 with Sonnet 4.0, 5 SWE-bench Verified tasks (astropy), 30 iterations max, 300s timeout. Three conditions:

1. **MCP (`readOnlyHint=true`)** — server-filesystem with native annotations, instrumented proxy for timing
2. **MCP (`readOnlyHint=false`)** — same proxy forces all tools to `readOnlyHint: false`, serializing execution
3. **Baseline** — native Claude Code tools only, no MCP

Parallelism measured by grouping `tool_use` blocks by `message.id` in `.mcp.log` traces — multiple tools per message = model requested parallel execution. IPC overhead measured by an instrumented proxy timing each JSON-RPC round-trip.

### Limitations

- n=5, single repo (astropy), single model (Sonnet 4.0), single MCP server
- No baseline parallelism data (`.mcp.log` only captures MCP sessions)
- MCP prompt encourages parallel tool use, which may inflate rates

## Reproduction

```bash
git clone https://github.com/greynewell/mcp-serialization-repro.git
cd mcp-serialization-repro

# Prerequisites: Docker, Node.js, Python 3.10+, ANTHROPIC_API_KEY
pip install mcpbr
bash run.sh            # ~$10-15 in API calls
python analyze.py results/
```

## References

- [MCP Specification](https://spec.modelcontextprotocol.io/) · [Claude Code #14353](https://github.com/anthropics/claude-code/issues/14353) · [SWE-bench](https://arxiv.org/abs/2310.06770) · [server-filesystem](https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem) · [mcpbr](https://pypi.org/project/mcpbr/)
