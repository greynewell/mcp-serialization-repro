#!/usr/bin/env node
/**
 * MCP proxy that forces readOnlyHint: false on all tools from
 * @modelcontextprotocol/server-filesystem.
 *
 * This causes Claude Code's isConcurrencySafe() to return false for every
 * tool, forcing serialized execution. Used as the control condition to
 * demonstrate that readOnlyHint annotations drive parallelism behavior.
 *
 * Wire format: NDJSON (newline-delimited JSON-RPC 2.0) per MCP stdio spec.
 *
 * Usage (standalone):
 *   node proxy/no-readonly-proxy.mjs /path/to/allowed/dir
 *
 * Usage (via mcpbr config):
 *   mcp_server:
 *     command: "node"
 *     args: ["proxy/no-readonly-proxy.mjs", "{workdir}"]
 */

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const args = process.argv.slice(2);

if (args.length === 0) {
  process.stderr.write(
    "Usage: no-readonly-proxy.mjs <allowed-directory> [...]\n"
  );
  process.exit(1);
}

// Spawn the real filesystem MCP server
const server = spawn(
  "npx",
  ["-y", "@modelcontextprotocol/server-filesystem", ...args],
  { stdio: ["pipe", "pipe", "inherit"] }
);

// Forward client stdin → server stdin unchanged
process.stdin.pipe(server.stdin);

// Intercept server stdout → client stdout, modifying tools/list responses
const rl = createInterface({ input: server.stdout, crlfDelay: Infinity });

rl.on("line", (line) => {
  if (!line.trim()) return;

  try {
    const msg = JSON.parse(line);

    // Intercept tools/list response: force readOnlyHint to false on all tools
    if (msg.result && Array.isArray(msg.result.tools)) {
      for (const tool of msg.result.tools) {
        if (!tool.annotations) {
          tool.annotations = {};
        }
        // Force all tools to appear non-readonly, causing serialization
        tool.annotations.readOnlyHint = false;
      }
      process.stderr.write(
        `[no-readonly-proxy] Stripped readOnlyHint from ${msg.result.tools.length} tools\n`
      );
    }

    process.stdout.write(JSON.stringify(msg) + "\n");
  } catch {
    // Not valid JSON — pass through as-is
    process.stdout.write(line + "\n");
  }
});

// Clean shutdown
server.on("exit", (code) => process.exit(code ?? 1));
server.on("error", (err) => {
  process.stderr.write(`[no-readonly-proxy] Server error: ${err.message}\n`);
  process.exit(1);
});
process.on("SIGTERM", () => server.kill("SIGTERM"));
process.on("SIGINT", () => server.kill("SIGINT"));
