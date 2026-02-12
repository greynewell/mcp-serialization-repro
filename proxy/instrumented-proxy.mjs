#!/usr/bin/env node
/**
 * Instrumented MCP proxy for @modelcontextprotocol/server-filesystem.
 *
 * Features:
 * 1. Per-tool-call timing: logs request->response round-trip time for each
 *    tools/call invocation. Timing is written to a JSONL file in the proxy
 *    directory (co-located with this script).
 *
 * 2. Optional readOnlyHint stripping: with --no-readonly flag, forces
 *    readOnlyHint: false on all tools in tools/list responses, causing
 *    Claude Code to serialize all MCP tool calls.
 *
 * Wire format: NDJSON (newline-delimited JSON-RPC 2.0) per MCP stdio spec.
 *
 * Usage:
 *   node proxy/instrumented-proxy.mjs /path/to/dir
 *   node proxy/instrumented-proxy.mjs --no-readonly /path/to/dir
 *
 * Timing output:
 *   Written to proxy/timing-<pid>.jsonl as structured JSONL.
 *   Each line: {"tool":"read_text_file","duration_ms":42.3,"request_id":1}
 *   Final line: {"type":"summary","total_calls":15,"avg_duration_ms":38.2,...}
 */

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { performance } from "node:perf_hooks";
import { createWriteStream } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Parse flags and directory args
const rawArgs = process.argv.slice(2);
const noReadonly = rawArgs.includes("--no-readonly");
const dirArgs = rawArgs.filter((a) => a !== "--no-readonly");

if (dirArgs.length === 0) {
  process.stderr.write(
    "Usage: instrumented-proxy.mjs [--no-readonly] <allowed-directory> [...]\n"
  );
  process.exit(1);
}

const mode = noReadonly ? "no-readonly" : "instrumented";

// Create timing log file with unique name (PID + timestamp + random)
const proxyDir = dirname(fileURLToPath(import.meta.url));
const uid = `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
const timingLogPath = join(proxyDir, `timing-${uid}.jsonl`);
const timingLog = createWriteStream(timingLogPath, { flags: "w" });

process.stderr.write(`[PROXY] Starting in ${mode} mode, PID=${process.pid}\n`);
process.stderr.write(`[PROXY] Timing log: ${timingLogPath}\n`);

// Spawn the real filesystem MCP server
const server = spawn(
  "npx",
  ["-y", "@modelcontextprotocol/server-filesystem", ...dirArgs],
  { stdio: ["pipe", "pipe", "inherit"] }
);

// Track pending tool calls for timing
// Map<request_id, {tool: string, start: number}>
const pendingCalls = new Map();

// === Intercept client stdin -> server stdin ===
const stdinRl = createInterface({ input: process.stdin, crlfDelay: Infinity });

stdinRl.on("line", (line) => {
  if (!line.trim()) return;

  try {
    const msg = JSON.parse(line);

    // Detect tools/call requests and record start time
    if (msg.method === "tools/call" && msg.id !== undefined) {
      const toolName = msg.params?.name || "unknown";
      pendingCalls.set(msg.id, {
        tool: toolName,
        start: performance.now(),
      });
    }

    // Forward to server unchanged
    server.stdin.write(JSON.stringify(msg) + "\n");
  } catch {
    server.stdin.write(line + "\n");
  }
});

stdinRl.on("close", () => {
  server.stdin.end();
});

// === Intercept server stdout -> client stdout ===
const stdoutRl = createInterface({
  input: server.stdout,
  crlfDelay: Infinity,
});

// Aggregate timing stats
let totalCalls = 0;
let totalDurationMs = 0;

stdoutRl.on("line", (line) => {
  if (!line.trim()) return;

  try {
    const msg = JSON.parse(line);

    // Check if this is a response to a pending tools/call
    if (msg.id !== undefined && pendingCalls.has(msg.id)) {
      const pending = pendingCalls.get(msg.id);
      pendingCalls.delete(msg.id);

      const durationMs = performance.now() - pending.start;
      totalCalls++;
      totalDurationMs += durationMs;

      // Write structured timing data to file
      const timing = {
        tool: pending.tool,
        duration_ms: Math.round(durationMs * 100) / 100,
        request_id: msg.id,
        is_error: !!msg.error,
      };
      timingLog.write(JSON.stringify(timing) + "\n");
    }

    // Optionally strip readOnlyHint from tools/list response
    if (noReadonly && msg.result && Array.isArray(msg.result.tools)) {
      for (const tool of msg.result.tools) {
        if (!tool.annotations) {
          tool.annotations = {};
        }
        tool.annotations.readOnlyHint = false;
      }
      process.stderr.write(
        `[PROXY] Forced readOnlyHint=false on ${msg.result.tools.length} tools\n`
      );
    }

    process.stdout.write(JSON.stringify(msg) + "\n");
  } catch {
    process.stdout.write(line + "\n");
  }
});

// Log summary on exit
function logSummary() {
  if (totalCalls > 0) {
    const avgMs = Math.round((totalDurationMs / totalCalls) * 100) / 100;
    const summary = {
      type: "summary",
      total_calls: totalCalls,
      total_duration_ms: Math.round(totalDurationMs * 100) / 100,
      avg_duration_ms: avgMs,
      mode,
    };
    timingLog.write(JSON.stringify(summary) + "\n");
    process.stderr.write(
      `[PROXY_SUMMARY] ${JSON.stringify(summary)}\n`
    );
  }
  timingLog.end();
}

// Clean shutdown
server.on("exit", (code) => {
  logSummary();
  setTimeout(() => process.exit(code ?? 0), 100);
});
server.on("error", (err) => {
  process.stderr.write(`[PROXY] Server error: ${err.message}\n`);
  logSummary();
  process.exit(1);
});
process.on("SIGTERM", () => {
  logSummary();
  server.kill("SIGTERM");
});
process.on("SIGINT", () => {
  logSummary();
  server.kill("SIGINT");
});
