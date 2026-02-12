This directory contains the complete output from an mcpbr evaluation run.

Started: 2026-02-12 14:24:35
Config: v2.1.39-no-readonly.yaml
Benchmark: swe-bench-verified
Model: claude-sonnet-4-20250514
Provider: anthropic

Files:
- config.yaml: Configuration used for this run
- evaluation_state.json: Per-task results and state
- logs/: Detailed execution traces (MCP server logs)

To analyze results:
  mcpbr state --state-dir results/v2.1.39-no-readonly

To archive:
  tar -czf results.tar.gz v2.1.39-no-readonly
