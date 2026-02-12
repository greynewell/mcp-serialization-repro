#!/usr/bin/env python3
"""Analyze mcpbr results for MCP tool call parallelism study.

Computes parallelism, performance, and IPC overhead metrics from:
  - evaluation_state.json: task resolution, iteration counts, runtimes
  - .mcp.log files: message-ID-based parallelism (model-requested tool grouping)
  - proxy/timing-*.jsonl: per-tool-call IPC round-trip timing

Usage:
    python analyze.py results/
    python analyze.py results/v2.1.39
"""

import json
import sys
from pathlib import Path

# MCP tools with readOnlyHint: true (from @modelcontextprotocol/server-filesystem)
READONLY_MCP_TOOLS = {
    "mcp__filesystem__read_file",
    "mcp__filesystem__read_text_file",
    "mcp__filesystem__read_media_file",
    "mcp__filesystem__read_multiple_files",
    "mcp__filesystem__list_directory",
    "mcp__filesystem__list_directory_with_sizes",
    "mcp__filesystem__directory_tree",
    "mcp__filesystem__search_files",
    "mcp__filesystem__get_file_info",
    "mcp__filesystem__list_allowed_directories",
}

# Tools that are harness overhead, not real agent work
OVERHEAD_TOOLS = {"TodoWrite", "Task", "TaskOutput", "EnterPlanMode", "ExitPlanMode"}


def is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__")


def is_readonly_mcp(name: str) -> bool:
    return name in READONLY_MCP_TOOLS


def load_evaluation_state(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0


def median_val(values: list[float]) -> float:
    if not values:
        return 0
    s = sorted(values)
    return s[len(s) // 2]


def p95_val(values: list[float]) -> float:
    if not values:
        return 0
    s = sorted(values)
    return s[int(len(s) * 0.95)]


# ---------------------------------------------------------------------------
# Parallelism analysis from .mcp.log files
# ---------------------------------------------------------------------------

def analyze_mcp_logs(log_dir: Path) -> list[dict] | None:
    """Analyze .mcp.log files for message-ID-based parallelism metrics.

    Each assistant event in the .mcp.log has a message.id. Multiple tool_use
    blocks sharing the same message.id were part of the same API response,
    meaning the model requested them in parallel.

    Only MCP runs produce .mcp.log files (baselines don't use MCP).

    Returns list of per-task parallelism dicts, or None if no logs found.
    """
    # .mcp.log files have colons in names: taskid:mcp_mcp.log
    mcp_logs = sorted(log_dir.glob("*mcp.log"))
    if not mcp_logs:
        return None

    results = []
    for log_file in mcp_logs:
        task_id = log_file.name.split(":")[0]

        # Parse [STDOUT] lines for assistant events
        tools_by_msg: dict[str, list[str]] = {}
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line.startswith("[STDOUT] "):
                    continue
                line = line[9:]
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if event.get("type") != "assistant":
                    continue
                msg = event.get("message", {})
                msg_id = msg.get("id", "")
                if not msg_id:
                    continue
                for block in msg.get("content", []):
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        if tool_name not in OVERHEAD_TOOLS:
                            tools_by_msg.setdefault(msg_id, []).append(tool_name)

        if not tools_by_msg:
            continue

        total_tools = sum(len(t) for t in tools_by_msg.values())
        total_msgs = len(tools_by_msg)
        multi_msgs = sum(1 for t in tools_by_msg.values() if len(t) > 1)

        # Count messages where 2+ MCP tools appear together (parallel MCP)
        mcp_parallel = 0
        # Count messages where 2+ readOnlyHint MCP tools appear together
        ro_parallel = 0
        for tools in tools_by_msg.values():
            mcp_count = sum(1 for t in tools if is_mcp_tool(t))
            ro_count = sum(1 for t in tools if is_readonly_mcp(t))
            if mcp_count >= 2:
                mcp_parallel += 1
            if ro_count >= 2:
                ro_parallel += 1

        results.append({
            "task": task_id,
            "tools_per_msg": total_tools / total_msgs,
            "multi_pct": multi_msgs / total_msgs * 100,
            "multi_msgs": multi_msgs,
            "total_msgs": total_msgs,
            "total_tools": total_tools,
            "mcp_parallel": mcp_parallel,
            "ro_parallel": ro_parallel,
            "tools_by_msg": tools_by_msg,  # Keep raw data for examples
        })

    return results if results else None


# ---------------------------------------------------------------------------
# Proxy timing analysis
# ---------------------------------------------------------------------------

def analyze_proxy_timing(proxy_dir: Path, mode_filter: str | None = None) -> dict | None:
    """Parse proxy timing data from timing-*.jsonl files.

    Args:
        proxy_dir: Directory containing timing-*.jsonl files
        mode_filter: If set, only include files with this mode in summary
                     ("instrumented" or "no-readonly")

    Returns dict with per-tool timing stats, or None if no data found.
    """
    timing_files = sorted(proxy_dir.glob("timing-*.jsonl"))
    if not timing_files:
        return None

    all_timings: list[dict] = []
    summaries: list[dict] = []

    for timing_path in timing_files:
        file_entries = []
        file_summary = None
        with open(timing_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "summary":
                        file_summary = entry
                    elif "duration_ms" in entry:
                        file_entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    pass

        # Filter by mode if requested
        if mode_filter and file_summary:
            if file_summary.get("mode") != mode_filter:
                continue

        all_timings.extend(file_entries)
        if file_summary:
            summaries.append(file_summary)

    if not all_timings:
        return None

    # Aggregate by tool name
    by_tool: dict[str, list[float]] = {}
    for t in all_timings:
        tool = t.get("tool", "unknown")
        duration = t.get("duration_ms", 0)
        by_tool.setdefault(tool, []).append(duration)

    all_durations = sorted(t["duration_ms"] for t in all_timings if "duration_ms" in t)

    return {
        "all_timings": all_timings,
        "by_tool": by_tool,
        "all_durations": all_durations,
        "summaries": summaries,
        "total_calls": len(all_timings),
        "avg_ms": avg(all_durations),
        "median_ms": median_val(all_durations),
        "p95_ms": p95_val(all_durations),
        "total_overhead_ms": sum(all_durations),
    }


# ---------------------------------------------------------------------------
# Evaluation state analysis
# ---------------------------------------------------------------------------

def analyze_eval_state(state: dict) -> dict:
    """Extract performance metrics from evaluation_state.json."""
    tasks = state.get("tasks", {})
    mcp_runs = []
    baseline_runs = []

    for task_id, task in tasks.items():
        mcp = task.get("mcp_result")
        base = task.get("baseline_result")

        if mcp:
            iters = mcp.get("iterations", 0)
            calls = mcp.get("tool_calls", 0)
            runtime = mcp.get("runtime_seconds", 0)
            resolved = mcp.get("resolved", False)
            mcp_runs.append({
                "task": task_id,
                "iterations": iters,
                "tool_calls": calls,
                "tools_per_iter": calls / iters if iters > 0 else 0,
                "runtime": runtime,
                "resolved": resolved,
            })

        if base:
            iters = base.get("iterations", 0)
            calls = base.get("tool_calls", 0)
            runtime = base.get("runtime_seconds", 0)
            resolved = base.get("resolved", False)
            baseline_runs.append({
                "task": task_id,
                "iterations": iters,
                "tool_calls": calls,
                "tools_per_iter": calls / iters if iters > 0 else 0,
                "runtime": runtime,
                "resolved": resolved,
            })

    return {"mcp": mcp_runs, "baseline": baseline_runs}


# ---------------------------------------------------------------------------
# Printing functions
# ---------------------------------------------------------------------------

def print_section(title: str):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def print_parallelism_table(mcp_log_data: list[dict] | None, label: str = "MCP"):
    """Print parallelism metrics from .mcp.log data."""
    print(f"\n--- Table 1: Model-Requested Parallelism ({label}, from .mcp.log) ---\n")

    if mcp_log_data is None:
        print("  [No .mcp.log files found]")
        return

    header = (
        f"  {'Task':<30} {'Msgs':>5} {'Tools':>6} {'Multi':>6} "
        f"{'Multi%':>7} {'T/Msg':>6} {'MCP-Par':>8}"
    )
    print(header)
    print(f"  {'-' * 70}")

    for r in mcp_log_data:
        short = r["task"].replace("astropy__astropy-", "astropy-")
        print(
            f"  {short:<30} {r['total_msgs']:>5} {r['total_tools']:>6} "
            f"{r['multi_msgs']:>6} {r['multi_pct']:>6.1f}% {r['tools_per_msg']:>6.2f} "
            f"{r['mcp_parallel']:>8}"
        )

    # Aggregates
    total_msgs = sum(r["total_msgs"] for r in mcp_log_data)
    total_tools = sum(r["total_tools"] for r in mcp_log_data)
    total_multi = sum(r["multi_msgs"] for r in mcp_log_data)
    total_mcp_par = sum(r["mcp_parallel"] for r in mcp_log_data)
    agg_tpm = total_tools / total_msgs if total_msgs else 0
    agg_mpct = total_multi / total_msgs * 100 if total_msgs else 0

    print(f"  {'-' * 70}")
    print(
        f"  {'AGGREGATE':<30} {total_msgs:>5} {total_tools:>6} "
        f"{total_multi:>6} {agg_mpct:>6.1f}% {agg_tpm:>6.2f} "
        f"{total_mcp_par:>8}"
    )


def print_performance_table(eval_data: dict, label: str = ""):
    """Print performance metrics from evaluation_state.json."""
    suffix = f" ({label})" if label else ""
    print(f"\n--- Table 2: Performance{suffix} (from evaluation_state.json) ---\n")

    header = f"  {'Condition':<12} {'Tools/Iter':>11} {'Avg Runtime':>11} {'Resolved':>10}"
    print(header)
    print(f"  {'-' * 46}")

    for condition in ["mcp", "baseline"]:
        runs = eval_data.get(condition, [])
        if not runs:
            continue
        valid = [r for r in runs if r["iterations"] > 0]
        tpi = avg([r["tools_per_iter"] for r in valid]) if valid else 0
        runtime = avg([r["runtime"] for r in runs])
        resolved = sum(1 for r in runs if r["resolved"])
        total = len(runs)
        cond_label = "MCP" if condition == "mcp" else "Baseline"
        print(f"  {cond_label:<12} {tpi:>11.2f} {runtime:>10.0f}s {resolved:>4}/{total}")


def print_per_task_table(eval_data: dict, label: str = ""):
    """Print per-task breakdown."""
    suffix = f" ({label})" if label else ""
    print(f"\n--- Per-Task Breakdown{suffix} ---\n")

    tasks_seen = {}
    for condition in ["mcp", "baseline"]:
        for r in eval_data[condition]:
            tasks_seen.setdefault(r["task"], {})[condition] = r

    header = (
        f"  {'Task':<30} {'MCP t/i':>8} {'Base t/i':>9} "
        f"{'MCP res':>8} {'Base res':>9}"
    )
    print(header)
    print(f"  {'-' * 66}")

    for task_id in sorted(tasks_seen.keys()):
        info = tasks_seen[task_id]
        mcp = info.get("mcp", {})
        base = info.get("baseline", {})
        m_tpi = f"{mcp['tools_per_iter']:.2f}" if mcp.get("iterations", 0) > 0 else "n/a"
        b_tpi = f"{base['tools_per_iter']:.2f}" if base.get("iterations", 0) > 0 else "n/a"
        m_res = "Yes" if mcp.get("resolved") else "No"
        b_res = "Yes" if base.get("resolved") else "No"
        short = task_id.replace("astropy__astropy-", "astropy-")
        print(f"  {short:<30} {m_tpi:>8} {b_tpi:>9} {m_res:>8} {b_res:>9}")


def print_ipc_overhead_table(timing_data: dict | None, label: str = ""):
    """Print IPC overhead metrics from proxy timing."""
    suffix = f" ({label})" if label else ""
    print(f"\n--- Table 3: MCP IPC Overhead{suffix} (from instrumented proxy) ---\n")

    if timing_data is None:
        print("  [No proxy timing data found for this condition]")
        return

    print(f"  Total MCP tool calls timed:  {timing_data['total_calls']}")
    print(f"  Mean round-trip time:        {timing_data['avg_ms']:.1f} ms")
    print(f"  Median round-trip time:      {timing_data['median_ms']:.1f} ms")
    print(f"  P95 round-trip time:         {timing_data['p95_ms']:.1f} ms")
    print(f"  Total MCP IPC overhead:      {timing_data['total_overhead_ms'] / 1000:.1f}s")

    print(f"\n  {'Tool':<25} {'Count':>6} {'Mean ms':>9} {'Median':>8} {'P95':>8}")
    print(f"  {'-' * 58}")

    for tool_name in sorted(timing_data["by_tool"].keys()):
        durations = sorted(timing_data["by_tool"][tool_name])
        count = len(durations)
        mean = avg(durations)
        med = median_val(durations)
        p95 = p95_val(durations)
        print(f"  {tool_name:<25} {count:>6} {mean:>9.1f} {med:>8.1f} {p95:>8.1f}")


def print_annotations_table():
    """Print MCP server tool annotations reference."""
    print("\n--- Table 4: MCP Server Tool Annotations (@modelcontextprotocol/server-filesystem) ---\n")
    print(f"  {'Tool':<25} {'readOnlyHint':>13} {'Concurrent':>11}")
    print(f"  {'-' * 51}")
    annotations = [
        ("read_text_file", True),
        ("read_media_file", True),
        ("read_multiple_files", True),
        ("list_directory", True),
        ("directory_tree", True),
        ("search_files", True),
        ("get_file_info", True),
        ("write_file", False),
        ("edit_file", False),
        ("move_file", False),
        ("create_directory", False),
    ]
    for tool, readonly in annotations:
        safe = "Yes" if readonly else "No"
        print(f"  {tool:<25} {str(readonly).lower():>13} {safe:>11}")


# ---------------------------------------------------------------------------
# Cross-condition comparison
# ---------------------------------------------------------------------------

def print_cross_condition_comparison(results_path: Path, proxy_dir: Path):
    """Compare readOnlyHint=true vs readOnlyHint=false conditions."""
    standard_dir = None
    no_readonly_dir = None

    for d in results_path.iterdir():
        if not d.is_dir():
            continue
        if d.name.endswith("-no-readonly") and (d / "evaluation_state.json").exists():
            no_readonly_dir = d
        elif not d.name.endswith("-no-readonly") and (d / "evaluation_state.json").exists():
            standard_dir = d

    if not standard_dir or not no_readonly_dir:
        return

    print_section("Cross-Condition Comparison: readOnlyHint Effect")

    # Load data
    std_eval = analyze_eval_state(load_evaluation_state(standard_dir / "evaluation_state.json"))
    nro_eval = analyze_eval_state(load_evaluation_state(no_readonly_dir / "evaluation_state.json"))

    std_logs = analyze_mcp_logs(standard_dir / "logs")
    nro_logs = analyze_mcp_logs(no_readonly_dir / "logs")

    std_timing = analyze_proxy_timing(proxy_dir, mode_filter="instrumented")
    nro_timing = analyze_proxy_timing(proxy_dir, mode_filter="no-readonly")

    # --- Parallelism comparison ---
    print("\n--- Parallelism: readOnlyHint=true vs readOnlyHint=false ---\n")
    print("  Model-requested parallelism from .mcp.log files.")
    print("  (Multi-tool messages = model asked for 2+ tools in one API response)\n")

    header = f"  {'Condition':<22} {'Msgs':>5} {'Tools':>6} {'Multi':>6} {'Multi%':>7} {'T/Msg':>6} {'MCP-Par':>8}"
    print(header)
    print(f"  {'-' * 62}")

    for label, log_data in [
        ("MCP (RO=true)", std_logs),
        ("MCP (RO=false)", nro_logs),
    ]:
        if log_data is None:
            print(f"  {label:<22} {'[no data]':>40}")
            continue
        total_msgs = sum(r["total_msgs"] for r in log_data)
        total_tools = sum(r["total_tools"] for r in log_data)
        total_multi = sum(r["multi_msgs"] for r in log_data)
        total_mcp_par = sum(r["mcp_parallel"] for r in log_data)
        tpm = total_tools / total_msgs if total_msgs else 0
        mpct = total_multi / total_msgs * 100 if total_msgs else 0
        print(
            f"  {label:<22} {total_msgs:>5} {total_tools:>6} "
            f"{total_multi:>6} {mpct:>6.1f}% {tpm:>6.2f} {total_mcp_par:>8}"
        )

    # --- Performance comparison ---
    print("\n--- Performance: readOnlyHint=true vs readOnlyHint=false ---\n")
    header = f"  {'Condition':<22} {'Tools/Iter':>11} {'Avg Runtime':>11} {'Resolved':>10}"
    print(header)
    print(f"  {'-' * 56}")

    for label, eval_data, condition in [
        ("MCP (RO=true)", std_eval, "mcp"),
        ("MCP (RO=false)", nro_eval, "mcp"),
        ("Baseline (std)", std_eval, "baseline"),
        ("Baseline (no-ro)", nro_eval, "baseline"),
    ]:
        runs = eval_data.get(condition, [])
        if not runs:
            continue
        valid = [r for r in runs if r["iterations"] > 0]
        tpi = avg([r["tools_per_iter"] for r in valid]) if valid else 0
        runtime = avg([r["runtime"] for r in runs])
        resolved = sum(1 for r in runs if r["resolved"])
        total = len(runs)
        print(f"  {label:<22} {tpi:>11.2f} {runtime:>10.0f}s {resolved:>4}/{total}")

    # --- IPC timing comparison ---
    print("\n--- IPC Overhead: readOnlyHint=true vs readOnlyHint=false ---\n")
    header = f"  {'Condition':<22} {'Calls':>6} {'Mean ms':>9} {'Median':>8} {'P95':>8} {'Total':>8}"
    print(header)
    print(f"  {'-' * 63}")

    for label, td in [("MCP (RO=true)", std_timing), ("MCP (RO=false)", nro_timing)]:
        if td is None:
            print(f"  {label:<22} {'[no data]':>40}")
            continue
        total_s = f"{td['total_overhead_ms']/1000:.1f}s"
        print(
            f"  {label:<22} {td['total_calls']:>6} {td['avg_ms']:>9.1f} "
            f"{td['median_ms']:>8.1f} {td['p95_ms']:>8.1f} {total_s:>8}"
        )

    # --- Parallelism examples ---
    if std_logs:
        print("\n--- Examples of Parallel MCP Tool Requests (RO=true) ---\n")
        shown = 0
        for r in std_logs:
            for msg_id, tools in r["tools_by_msg"].items():
                mcp_tools = [t for t in tools if is_mcp_tool(t)]
                if len(mcp_tools) >= 2:
                    short_task = r["task"].replace("astropy__astropy-", "")
                    short_tools = [t.replace("mcp__filesystem__", "") for t in mcp_tools]
                    print(f"  Task {short_task}: {short_tools}")
                    shown += 1
                    if shown >= 8:
                        break
            if shown >= 8:
                break

    if nro_logs:
        print("\n--- Examples of Parallel MCP Tool Requests (RO=false) ---\n")
        shown = 0
        for r in nro_logs:
            for msg_id, tools in r["tools_by_msg"].items():
                mcp_tools = [t for t in tools if is_mcp_tool(t)]
                if len(mcp_tools) >= 2:
                    short_task = r["task"].replace("astropy__astropy-", "")
                    short_tools = [t.replace("mcp__filesystem__", "") for t in mcp_tools]
                    print(f"  Task {short_task}: {short_tools}")
                    shown += 1
                    if shown >= 8:
                        break
            if shown >= 8:
                break


# ---------------------------------------------------------------------------
# Per-version analysis
# ---------------------------------------------------------------------------

def analyze_version(version_dir: Path, proxy_dir: Path):
    """Analyze a single version's results."""
    eval_path = version_dir / "evaluation_state.json"
    log_dir = version_dir / "logs"

    if not eval_path.exists():
        print(f"  WARNING: {eval_path} not found, skipping.", file=sys.stderr)
        return

    state = load_evaluation_state(eval_path)
    eval_data = analyze_eval_state(state)

    # Determine proxy mode filter from directory name
    if "no-readonly" in version_dir.name:
        mode_filter = "no-readonly"
    else:
        mode_filter = "instrumented"

    mcp_log_data = analyze_mcp_logs(log_dir) if log_dir.exists() else None
    timing_data = analyze_proxy_timing(proxy_dir, mode_filter=mode_filter) if proxy_dir.exists() else None

    print_parallelism_table(mcp_log_data, label=version_dir.name)
    print_performance_table(eval_data, label=version_dir.name)
    print_per_task_table(eval_data, label=version_dir.name)
    print_ipc_overhead_table(timing_data, label=version_dir.name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze.py <results-dir>", file=sys.stderr)
        print("  e.g.: python analyze.py results/", file=sys.stderr)
        print("        python analyze.py results/v2.1.39", file=sys.stderr)
        sys.exit(1)

    results_path = Path(sys.argv[1])

    if not results_path.exists():
        print(f"ERROR: {results_path} not found.", file=sys.stderr)
        sys.exit(1)

    # Locate proxy directory (sibling of results/)
    proxy_dir = results_path.parent / "proxy"
    if not proxy_dir.exists():
        proxy_dir = results_path / ".." / "proxy"
        proxy_dir = proxy_dir.resolve()

    print("MCP Tool Call Parallelism Analysis")
    print("=" * 70)
    if proxy_dir.exists():
        timing_count = len(list(proxy_dir.glob("timing-*.jsonl")))
        print(f"Proxy timing files: {timing_count} in {proxy_dir}")
    else:
        print(f"Proxy directory: not found (looked at {proxy_dir})")

    # Check if this is a single version dir or parent of multiple
    eval_file = results_path / "evaluation_state.json"
    if eval_file.exists():
        # Single version directory
        print_section(results_path.name)
        analyze_version(results_path, proxy_dir)
    else:
        # Parent directory — look for version subdirectories
        version_dirs = sorted(
            d for d in results_path.iterdir()
            if d.is_dir() and (d / "evaluation_state.json").exists()
        )
        if not version_dirs:
            print(f"ERROR: No evaluation_state.json found in {results_path} "
                  f"or its subdirectories.", file=sys.stderr)
            sys.exit(1)

        print(f"Result sets: {', '.join(d.name for d in version_dirs)}")

        for vdir in version_dirs:
            print_section(vdir.name)
            analyze_version(vdir, proxy_dir)

        # Cross-condition comparison if both standard and no-readonly exist
        print_cross_condition_comparison(results_path, proxy_dir)

    # Reference tables
    print_annotations_table()

    print(f"\n{'=' * 70}")
    print("Analysis complete.")


if __name__ == "__main__":
    main()
