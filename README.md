# sysops-mcp

[![CI](https://github.com/Enrique-S-J/sysops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Enrique-S-J/sysops-mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server that gives AI assistants safe, read-only visibility into a host: CPU, memory, disk, processes, network, ports, and logs.

Built for the kind of first-pass triage an on-call engineer does before deciding whether a box needs attention. Ask your assistant "how is this server doing?" and it can actually go look.

```
You: hey, the app feels slow, can you check the box?

Assistant: [get_host_summary] -> CPU 4%, RAM 91%, swap 88%, fullest disk 62%
                                 "memory is nearly exhausted"
           [list_top_processes sort_by=memory] -> postgres using 71% RAM
           [tail_log /var/log/postgresql/postgresql.log grep="checkpoint"]

           Memory is the pressure point, not CPU. Postgres is holding 71% of
           RAM and the logs show frequent checkpoints...
```

## Tools

| Tool | What it does |
|------|--------------|
| `get_host_summary` | One-call health snapshot: uptime, CPU, RAM, swap, fullest disk, plain-language assessment. The right first call. |
| `get_cpu_status` | Utilization (configurable sample window), core counts, load averages, load-per-core saturation check. |
| `get_memory_status` | RAM and swap usage. |
| `get_disk_usage` | Per-partition usage with a configurable warning threshold. Read-only mounts are listed but never flagged. |
| `list_top_processes` | Top N processes by CPU or memory, with name filtering. |
| `get_network_status` | Interfaces, addresses, I/O totals, optional TCP connection states. |
| `check_port` | TCP connect test with latency, distinguishing refused vs. timed out vs. DNS failure. |
| `tail_log` | Last N lines of a system log, with substring filtering. Path-restricted (see below). |

Every tool supports `markdown` (human-readable) and `json` (machine-readable) output.

## Quickstart

Requires Python 3.10+.

```bash
git clone https://github.com/Enrique-S-J/sysops-mcp.git
cd sysops-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

The server speaks stdio and is launched by its client, so running `python server.py` yourself just blocks waiting for JSON-RPC on stdin. Point a client at it instead.

### Claude Code

```bash
claude mcp add sysops -- /absolute/path/to/sysops-mcp/.venv/bin/python \
                         /absolute/path/to/sysops-mcp/server.py
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sysops": {
      "command": "/absolute/path/to/sysops-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/sysops-mcp/server.py"]
    }
  }
}
```

Both paths must be absolute, and `command` must be the interpreter **inside the virtualenv**. A bare `python3` will start but fail on import, because the system interpreter has no `mcp` or `psutil`.

Works with any MCP client that supports stdio transport.

## Design notes

A few deliberate decisions, since diagnostics tooling handed to an LLM deserves some care:

- **Read-only by design.** No kill, no restart, no writes. An assistant can diagnose, a human decides what to do about it. Every tool is annotated `readOnlyHint: true` so clients can reason about safety.
- **`tail_log` is sandboxed.** Reads are restricted to allowed roots (`/var/log` by default) with symlinks resolved *before* the check, so a confused or prompt-injected client can't use it as an arbitrary file reader (`/var/log/../../etc/passwd` is rejected). Both sides of the containment check are resolved, because on macOS the root itself is a symlink.
- **Named arguments, not a wrapper object.** Each tool's parameters are individual schema properties carrying their own descriptions, rather than one nested object the model has to unpack. The descriptions are what the model reads to decide how to call a tool, so they belong on the arguments.
- **Only actionable signals raise alarms.** Read-only mounts can't have space reclaimed, and some are full by construction — every Ubuntu host with snaps carries squashfs mounts pinned at 100%. Counting those would report "a disk is nearly full" forever and train the reader to ignore it.
- **Errors teach the next step.** A failed port check tells you whether the connection was refused (host up, nothing listening) or timed out (host down or firewalled), because those imply different follow-ups. A bad disk path suggests calling the tool without arguments to list valid mounts.
- **Context-efficient responses.** Markdown output is trimmed for an LLM's context window: rounded numbers, human-readable timestamps, no metadata dumps. Full fidelity is available via `response_format: "json"`.
- **Interpretation included.** `get_host_summary` doesn't just return numbers; it computes load-per-core and returns an assessment ("memory is nearly exhausted"), so the model spends its reasoning on the problem rather than the arithmetic.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

37 tests across two suites:

- **`test_server.py`** — tool output shape, sorting/filtering, port-check outcomes (open, closed, DNS failure, via a real ephemeral listener), the log path sandbox including traversal attempts, and the summary's assessment thresholds against synthetic hosts (swapping, exhausted RAM, full read-only mounts).
- **`test_protocol.py`** — the MCP layer itself: that every parameter is advertised as a named property with its own description, that required arguments and read-only annotations survive serialization, and that the log sandbox and argument bounds hold on the real call path. Direct function calls can't catch a schema that no client can call correctly.

CI runs the suite on Linux and macOS across Python 3.10–3.13. Both operating systems are deliberate: this server has shipped two bugs that were invisible on one platform and broken on the other.

## Roadmap

- `get_service_status` (systemd unit states)
- Docker container visibility (when a socket is present)
- Streamable HTTP transport for remote hosts

## License

MIT
