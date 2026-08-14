"""sysops_mcp: an MCP server exposing system diagnostics to AI assistants.

Gives an LLM safe, read-only visibility into a host: CPU, memory, disk,
processes, network, ports, and logs. Designed for the kind of first-pass
triage an on-call engineer does before deciding whether a box needs attention.

Transport: stdio (run directly, or wire into Claude Desktop / any MCP client).
"""

from __future__ import annotations

import json
import shutil
import socket
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import psutil
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints

mcp = MCPServer(
    name="sysops_mcp",
    title="SysOps Diagnostics",
    description="Safe, read-only system diagnostics for AI assistants: CPU, memory, disk, processes, network, ports, and logs.",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

BYTES_IN_GB = 1024**3

# Log reads are restricted to these roots so a prompt-injected or confused
# client can't use tail_log as an arbitrary file reader (e.g. ~/.ssh, /etc/shadow).
ALLOWED_LOG_ROOTS = ("/var/log",)
MAX_LOG_LINES = 500


def _is_within_allowed_roots(resolved: Path) -> bool:
    """True if a fully-resolved path sits inside an allowed log root.

    The roots are resolved too, so platforms where a root is itself a symlink
    (macOS: /var -> /private/var) compare correctly. Both sides of a
    containment check must be resolved or legitimate paths get rejected.
    """
    for root in ALLOWED_LOG_ROOTS:
        root_resolved = Path(root).resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return True
    return False


# Tool parameters are declared individually rather than as a single Pydantic
# model argument. The SDK maps one function parameter to one schema property,
# so a lone model parameter would advertise every tool as taking a single
# opaque object; declaring fields directly gives clients named arguments.
StrippedStr = Annotated[str, StringConstraints(strip_whitespace=True)]

FORMAT_DESC = "'markdown' for human-readable output, 'json' for structured data."


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


def _gb(num_bytes: int) -> float:
    """Convert bytes to gigabytes, rounded for display."""
    return round(num_bytes / BYTES_IN_GB, 2)


def _ts(epoch: float) -> str:
    """Render an epoch timestamp as a readable UTC string."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _render(payload: dict, fmt: ResponseFormat, md_renderer) -> str:
    """Render a payload as JSON or via the tool's markdown renderer."""
    if fmt == ResponseFormat.JSON:
        return json.dumps(payload, indent=2, default=str)
    return md_renderer(payload)


# ---------------------------------------------------------------------------
# CPU / load
# ---------------------------------------------------------------------------


def _cpu_markdown(d: dict) -> str:
    lines = [
        "## CPU Status",
        f"- Utilization: **{d['percent']}%** over {d['sample_seconds']}s sample",
        f"- Cores: {d['physical_cores']} physical / {d['logical_cores']} logical",
        f"- Load average (1/5/15 min): {d['load_avg']['1min']} / {d['load_avg']['5min']} / {d['load_avg']['15min']}",
        f"- Load per core (1 min): {d['load_per_core_1min']} "
        f"({'saturated' if d['load_per_core_1min'] >= 1.0 else 'headroom available'})",
    ]
    if d.get("per_core_percent") is not None:
        cores = ", ".join(f"cpu{i}: {p}%" for i, p in enumerate(d["per_core_percent"]))
        lines.append(f"- Per-core: {cores}")
    return "\n".join(lines)


@mcp.tool(
    name="get_cpu_status",
    annotations=ToolAnnotations(
        title="Get CPU Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_cpu_status(
    sample_seconds: Annotated[
        float,
        Field(
            description="How long to sample CPU utilization (e.g. 1.0). "
            "Longer samples smooth spikes.",
            ge=0.1,
            le=10.0,
        ),
    ] = 1.0,
    per_core: Annotated[
        bool, Field(description="Include per-core utilization breakdown.")
    ] = False,
    response_format: Annotated[ResponseFormat, Field(description=FORMAT_DESC)] = (
        ResponseFormat.MARKDOWN
    ),
) -> str:
    """Get CPU utilization, core counts, and load averages.

    Samples CPU for `sample_seconds` and reports utilization percentage,
    physical/logical core counts, 1/5/15-minute load averages, and
    load-per-core (values >= 1.0 indicate saturation).

    Returns:
        str: Markdown summary or JSON object with keys: percent,
        sample_seconds, physical_cores, logical_cores, load_avg
        {1min,5min,15min}, load_per_core_1min, per_core_percent (optional).
    """
    percent = psutil.cpu_percent(interval=sample_seconds)
    per_core_percent = (
        psutil.cpu_percent(interval=0.1, percpu=True) if per_core else None
    )
    la1, la5, la15 = psutil.getloadavg()
    logical = psutil.cpu_count(logical=True) or 1
    payload = {
        "percent": percent,
        "sample_seconds": sample_seconds,
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": logical,
        "load_avg": {"1min": round(la1, 2), "5min": round(la5, 2), "15min": round(la15, 2)},
        "load_per_core_1min": round(la1 / logical, 2),
        "per_core_percent": per_core_percent,
    }
    return _render(payload, response_format, _cpu_markdown)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _memory_markdown(d: dict) -> str:
    v, s = d["virtual"], d["swap"]
    return "\n".join(
        [
            "## Memory Status",
            f"- RAM: **{v['used_gb']} / {v['total_gb']} GB used ({v['percent']}%)**, "
            f"{v['available_gb']} GB available",
            f"- Swap: {s['used_gb']} / {s['total_gb']} GB used ({s['percent']}%)",
        ]
    )


@mcp.tool(
    name="get_memory_status",
    annotations=ToolAnnotations(
        title="Get Memory Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_memory_status(
    response_format: Annotated[ResponseFormat, Field(description=FORMAT_DESC)] = (
        ResponseFormat.MARKDOWN
    ),
) -> str:
    """Get RAM and swap utilization.

    Returns:
        str: Markdown summary or JSON object with keys: virtual
        {total_gb,used_gb,available_gb,percent} and swap
        {total_gb,used_gb,percent}.
    """
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    payload = {
        "virtual": {
            "total_gb": _gb(vm.total),
            "used_gb": _gb(vm.used),
            "available_gb": _gb(vm.available),
            "percent": vm.percent,
        },
        "swap": {"total_gb": _gb(sw.total), "used_gb": _gb(sw.used), "percent": sw.percent},
    }
    return _render(payload, response_format, _memory_markdown)


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------


def _disk_markdown(d: dict) -> str:
    lines = ["## Disk Usage"]
    for p in d["partitions"]:
        flag = " ⚠️ **WARNING**" if p["warning"] else ""
        lines.append(
            f"- `{p['mountpoint']}` ({p['device']}, {p['fstype']}): "
            f"{p['used_gb']} / {p['total_gb']} GB used ({p['percent']}%){flag}"
        )
    if d["warnings"]:
        lines.append(f"\n{d['warnings']} partition(s) above {d['warn_percent']}% usage.")
    return "\n".join(lines)


@mcp.tool(
    name="get_disk_usage",
    annotations=ToolAnnotations(
        title="Get Disk Usage",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_disk_usage(
    path: Annotated[
        Optional[StrippedStr],
        Field(
            description="Specific mount point or path to inspect (e.g. '/', '/var'). "
            "Omit to list all mounted partitions.",
            max_length=4096,
        ),
    ] = None,
    warn_percent: Annotated[
        float,
        Field(
            description="Usage percentage above which a partition is flagged as WARNING.",
            ge=1.0,
            le=100.0,
        ),
    ] = 85.0,
    response_format: Annotated[ResponseFormat, Field(description=FORMAT_DESC)] = (
        ResponseFormat.MARKDOWN
    ),
) -> str:
    """Get disk usage per partition, flagging any above a warning threshold.

    Returns:
        str: Markdown summary or JSON object with keys: partitions (list of
        {mountpoint,device,fstype,total_gb,used_gb,percent,warning}),
        warnings (count), warn_percent.
    """
    targets = []
    if path:
        if not Path(path).exists():
            return (
                f"Error: path '{path}' does not exist. "
                "Call get_disk_usage without a path to list available mount points."
            )
        targets.append((path, path, "-"))
    else:
        for part in psutil.disk_partitions(all=False):
            targets.append((part.mountpoint, part.device, part.fstype))

    partitions = []
    for mountpoint, device, fstype in targets:
        try:
            usage = psutil.disk_usage(mountpoint)
        except PermissionError:
            continue
        partitions.append(
            {
                "mountpoint": mountpoint,
                "device": device,
                "fstype": fstype,
                "total_gb": _gb(usage.total),
                "used_gb": _gb(usage.used),
                "percent": usage.percent,
                "warning": usage.percent >= warn_percent,
            }
        )
    payload = {
        "partitions": partitions,
        "warnings": sum(1 for p in partitions if p["warning"]),
        "warn_percent": warn_percent,
    }
    return _render(payload, response_format, _disk_markdown)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


class SortBy(str, Enum):
    """Process sort key."""

    CPU = "cpu"
    MEMORY = "memory"


def _proc_markdown(d: dict) -> str:
    lines = [
        f"## Top {d['count']} Processes by {d['sort_by'].upper()}",
        "",
        "| PID | Name | CPU % | Mem % | User | Started |",
        "|-----|------|-------|-------|------|---------|",
    ]
    for p in d["processes"]:
        lines.append(
            f"| {p['pid']} | {p['name']} | {p['cpu_percent']} | "
            f"{p['memory_percent']} | {p['username']} | {p['started']} |"
        )
    return "\n".join(lines)


@mcp.tool(
    name="list_top_processes",
    annotations=ToolAnnotations(
        title="List Top Processes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def list_top_processes(
    sort_by: Annotated[
        SortBy, Field(description="Sort processes by 'cpu' or 'memory'.")
    ] = SortBy.CPU,
    limit: Annotated[
        int, Field(description="Number of processes to return.", ge=1, le=50)
    ] = 10,
    name_filter: Annotated[
        Optional[StrippedStr],
        Field(
            description="Case-insensitive substring to filter process names (e.g. 'python').",
            max_length=100,
        ),
    ] = None,
    response_format: Annotated[ResponseFormat, Field(description=FORMAT_DESC)] = (
        ResponseFormat.MARKDOWN
    ),
) -> str:
    """List the top processes by CPU or memory consumption.

    Returns:
        str: Markdown table or JSON object with keys: count, sort_by,
        processes (list of {pid,name,cpu_percent,memory_percent,username,started}).
    """
    procs = []
    for proc in psutil.process_iter(["pid", "name", "username", "memory_percent", "create_time"]):
        try:
            info = proc.info
            if name_filter and name_filter.lower() not in (info["name"] or "").lower():
                continue
            info["cpu_percent"] = proc.cpu_percent(interval=None)
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Second pass over a short interval gives meaningful cpu_percent values.
    time.sleep(0.1)
    by_pid = {p["pid"]: p for p in procs}
    for proc in psutil.process_iter(["pid"]):
        try:
            if proc.pid in by_pid:
                by_pid[proc.pid]["cpu_percent"] = proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = "cpu_percent" if sort_by == SortBy.CPU else "memory_percent"
    procs.sort(key=lambda p: p.get(key) or 0, reverse=True)
    top = [
        {
            "pid": p["pid"],
            "name": p["name"],
            "cpu_percent": round(p.get("cpu_percent") or 0, 1),
            "memory_percent": round(p.get("memory_percent") or 0, 1),
            "username": p.get("username") or "-",
            "started": _ts(p["create_time"]) if p.get("create_time") else "-",
        }
        for p in procs[:limit]
    ]
    payload = {"count": len(top), "sort_by": sort_by.value, "processes": top}
    return _render(payload, response_format, _proc_markdown)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def _net_markdown(d: dict) -> str:
    lines = ["## Network Status"]
    for name, iface in d["interfaces"].items():
        addrs = ", ".join(iface["addresses"]) or "no address"
        state = "up" if iface["is_up"] else "down"
        lines.append(f"- `{name}` ({state}): {addrs}")
    io = d["io_totals"]
    lines.append(
        f"- Totals since boot: {io['sent_gb']} GB sent / {io['recv_gb']} GB received"
    )
    if d.get("tcp_states"):
        states = ", ".join(f"{k}: {v}" for k, v in d["tcp_states"].items())
        lines.append(f"- TCP connections: {states}")
    return "\n".join(lines)


@mcp.tool(
    name="get_network_status",
    annotations=ToolAnnotations(
        title="Get Network Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def get_network_status(
    include_connections: Annotated[
        bool,
        Field(
            description="Include counts of TCP connections by state "
            "(ESTABLISHED, LISTEN, ...).",
        ),
    ] = False,
    response_format: Annotated[ResponseFormat, Field(description=FORMAT_DESC)] = (
        ResponseFormat.MARKDOWN
    ),
) -> str:
    """Get network interfaces, their addresses, and I/O totals since boot.

    Optionally includes TCP connection counts grouped by state, which is
    useful for spotting connection leaks or unexpected listeners.

    Returns:
        str: Markdown summary or JSON object with keys: interfaces
        ({name: {is_up, addresses[]}}), io_totals {sent_gb,recv_gb},
        tcp_states (optional {state: count}).
    """
    interfaces = {}
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        ipv4 = [a.address for a in addrs if a.family == socket.AF_INET]
        interfaces[name] = {
            "is_up": stats[name].isup if name in stats else False,
            "addresses": ipv4,
        }
    io = psutil.net_io_counters()
    payload = {
        "interfaces": interfaces,
        "io_totals": {"sent_gb": _gb(io.bytes_sent), "recv_gb": _gb(io.bytes_recv)},
    }
    if include_connections:
        states: dict[str, int] = {}
        try:
            for conn in psutil.net_connections(kind="tcp"):
                states[conn.status] = states.get(conn.status, 0) + 1
            payload["tcp_states"] = dict(sorted(states.items()))
        except psutil.AccessDenied:
            payload["tcp_states"] = {
                "error": "Access denied. Re-run with elevated privileges to see connection states."
            }
    return _render(payload, response_format, _net_markdown)


# ---------------------------------------------------------------------------
# Port check
# ---------------------------------------------------------------------------


@mcp.tool(
    name="check_port",
    annotations=ToolAnnotations(
        title="Check TCP Port",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def check_port(
    port: Annotated[
        int,
        Field(description="TCP port number to test (e.g. 22, 443, 5432).", ge=1, le=65535),
    ],
    host: Annotated[
        StrippedStr,
        Field(
            description="Hostname or IP to check (e.g. '127.0.0.1', 'db.internal').",
            min_length=1,
            max_length=253,
        ),
    ] = "127.0.0.1",
    timeout_seconds: Annotated[
        float, Field(description="Connection timeout in seconds.", ge=0.1, le=30.0)
    ] = 3.0,
) -> str:
    """Test whether a TCP port is accepting connections.

    Performs a TCP connect to host:port with a timeout. Useful for
    verifying a service is listening ("is Postgres up on 5432?") before
    deeper diagnosis.

    Returns:
        str: One-line result: open, closed/refused, timed out, or
        DNS resolution failure, with latency when the port is open.
    """
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            ms = round((time.monotonic() - start) * 1000, 1)
            return f"Port {port} on {host} is OPEN (connected in {ms} ms)."
    except socket.gaierror:
        return (
            f"Error: could not resolve host '{host}'. "
            "Check the hostname or try an IP address."
        )
    except ConnectionRefusedError:
        return (
            f"Port {port} on {host} is CLOSED (connection refused). "
            "The host is reachable but nothing is listening on that port."
        )
    except (socket.timeout, TimeoutError, OSError):
        return (
            f"Port {port} on {host} TIMED OUT after {timeout_seconds}s. "
            "The host may be down, unreachable, or a firewall may be dropping packets."
        )


# ---------------------------------------------------------------------------
# Uptime / host summary
# ---------------------------------------------------------------------------


def _host_markdown(d: dict) -> str:
    return "\n".join(
        [
            "## Host Summary",
            f"- Hostname: **{d['hostname']}**",
            f"- Booted: {d['boot_time']} (up {d['uptime_hours']} hours)",
            f"- CPU: {d['cpu_percent']}% | RAM: {d['memory_percent']}% | "
            f"Fullest disk: {d['max_disk_percent']}%",
            f"- Quick read: {d['assessment']}",
        ]
    )


@mcp.tool(
    name="get_host_summary",
    annotations=ToolAnnotations(
        title="Get Host Summary",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def get_host_summary(
    response_format: Annotated[ResponseFormat, Field(description=FORMAT_DESC)] = (
        ResponseFormat.MARKDOWN
    ),
) -> str:
    """One-call health snapshot: hostname, uptime, CPU, RAM, fullest disk.

    The right first tool to call when asked "how is this box doing?" —
    it aggregates the key signals and gives a plain-language assessment,
    so follow-up tools only need to be called where something looks off.

    Returns:
        str: Markdown summary or JSON object with keys: hostname, boot_time,
        uptime_hours, cpu_percent, memory_percent, max_disk_percent, assessment.
    """
    boot = psutil.boot_time()
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory().percent
    max_disk = 0.0
    for part in psutil.disk_partitions(all=False):
        try:
            max_disk = max(max_disk, psutil.disk_usage(part.mountpoint).percent)
        except PermissionError:
            continue

    issues = []
    if cpu >= 90:
        issues.append("CPU is saturated")
    if mem >= 90:
        issues.append("memory is nearly exhausted")
    if max_disk >= 90:
        issues.append("a disk is nearly full")
    assessment = "; ".join(issues) if issues else "no immediate resource pressure detected"

    payload = {
        "hostname": socket.gethostname(),
        "boot_time": _ts(boot),
        "uptime_hours": round((time.time() - boot) / 3600, 1),
        "cpu_percent": cpu,
        "memory_percent": mem,
        "max_disk_percent": max_disk,
        "assessment": assessment,
    }
    return _render(payload, response_format, _host_markdown)


# ---------------------------------------------------------------------------
# Log tail
# ---------------------------------------------------------------------------


@mcp.tool(
    name="tail_log",
    annotations=ToolAnnotations(
        title="Tail a Log File",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def tail_log(
    path: Annotated[
        StrippedStr,
        Field(
            description="Absolute path to a log file under an allowed root "
            f"({', '.join(ALLOWED_LOG_ROOTS)}), e.g. '/var/log/syslog'.",
            min_length=1,
            max_length=4096,
        ),
    ],
    lines: Annotated[
        int,
        Field(
            description="Number of lines from the end of the file.", ge=1, le=MAX_LOG_LINES
        ),
    ] = 50,
    grep: Annotated[
        Optional[StrippedStr],
        Field(
            description="Case-insensitive substring filter applied before the line limit "
            "(e.g. 'error', 'oom').",
            max_length=200,
        ),
    ] = None,
) -> str:
    """Read the last N lines of a log file, optionally filtered by substring.

    For safety, reads are restricted to allowed log roots (default: /var/log)
    and capped at 500 lines. Symlinks are resolved before the check, so
    links pointing outside the allowed roots are rejected.

    Returns:
        str: The matching log lines, or an actionable error message.
    """
    resolved = Path(path).resolve()
    if not _is_within_allowed_roots(resolved):
        return (
            f"Error: '{path}' resolves outside the allowed log roots "
            f"({', '.join(ALLOWED_LOG_ROOTS)}). This tool only reads system logs."
        )
    if not resolved.exists():
        return f"Error: log file '{resolved}' does not exist."
    if not resolved.is_file():
        return f"Error: '{resolved}' is not a regular file."

    try:
        with open(resolved, "r", errors="replace") as f:
            all_lines = f.readlines()
    except PermissionError:
        return (
            f"Error: permission denied reading '{resolved}'. "
            "The server may need to run as a user with log read access."
        )

    if grep:
        needle = grep.lower()
        all_lines = [ln for ln in all_lines if needle in ln.lower()]

    tail = all_lines[-lines:]
    if not tail:
        filt = f" matching '{grep}'" if grep else ""
        return f"No lines{filt} found in {resolved}."
    header = f"Last {len(tail)} line(s) of {resolved}"
    if grep:
        header += f" matching '{grep}'"
    return header + ":\n\n" + "".join(tail)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
