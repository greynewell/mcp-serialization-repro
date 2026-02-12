#!/usr/bin/env bash
set -euo pipefail

echo "=== MCP Tool Call Parallelism Study ==="
echo ""

# Check for ANTHROPIC_API_KEY
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY environment variable is not set."
    echo "  export ANTHROPIC_API_KEY='your-key-here'"
    exit 1
fi
echo "[OK] ANTHROPIC_API_KEY is set"

# Check Docker is running
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is not running. Please start Docker Desktop."
    exit 1
fi
echo "[OK] Docker is running"

# Check mcpbr is installed
if ! command -v mcpbr >/dev/null 2>&1; then
    echo "ERROR: mcpbr is not installed."
    echo "  pip install mcpbr"
    exit 1
fi
echo "[OK] mcpbr is installed ($(mcpbr --version 2>/dev/null || echo 'unknown version'))"

# Check npx is available
if ! command -v npx >/dev/null 2>&1; then
    echo "ERROR: npx is not available. Please install Node.js."
    exit 1
fi
echo "[OK] npx is available"

echo ""

VERSION="2.1.39"
export CLAUDE_CODE_VERSION="$VERSION"

# --- Experiment 1: MCP with proper annotations (instrumented for timing) + Baseline ---
config="configs/v${VERSION}.yaml"
echo "=== Experiment 1: MCP (readOnlyHint=true) + Baseline ==="
echo "  Config: $config"
echo "  MCP server: instrumented-proxy.mjs (timing enabled)"
echo "  Output: results/v${VERSION}/"
echo ""
mcpbr run -c "$config"
echo ""
echo "=== Experiment 1 complete ==="
echo ""

# --- Experiment 2: MCP with readOnlyHint forced false (serialization control) ---
config_nro="configs/v${VERSION}-no-readonly.yaml"
echo "=== Experiment 2: MCP (readOnlyHint=false, forced serialization) + Baseline ==="
echo "  Config: $config_nro"
echo "  MCP server: instrumented-proxy.mjs --no-readonly"
echo "  Output: results/v${VERSION}-no-readonly/"
echo ""
mcpbr run -c "$config_nro"
echo ""
echo "=== Experiment 2 complete ==="
echo ""

echo "=== All experiments complete ==="
echo "Results saved to ./results/"
echo "Run 'python analyze.py results/' to generate analysis tables."
