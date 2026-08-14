# sysops-mcp

An [MCP](https://modelcontextprotocol.io) server that gives AI assistants safe, read-only visibility into a host: CPU, memory, disk, processes, network, ports, and logs.

Built for the kind of first-pass triage an on-call engineer does before deciding whether a box needs attention. Ask your assistant "how is this server doing?" and it can actually go look.

```
You: hey, the app feels slow, can you check the box?

Assistant: [get_host_summary] -> CPU 4%, RAM 91%, fullest disk 62%
           [list_top_processes sort_by=memory] -> postgres using 71% RAM
           [tail_log /var/log/postgresql/postgresql.log grep="checkpoint"]

           Memory is the pressure point, not CPU. Postgres is holding 71% of
           RAM and the logs show frequent checkpoints...
```

## Tools

| Tool | What it does |
|------|--------------|
| `get_host_summary` | One-call health snapshot: uptime, CPU, RAM, fullest disk, plain-language assessment. The right first call. |
| `get_cpu_status` | Utilization (configurable sample window), core counts, load averages, load-per-core saturation check. |
| `get_memory_status` | RAM and swap usage. |
| `get_disk_usage` | Per-partition usage with a configurable warning threshold. |
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
pip install -e .
python server.py   # runs on stdio
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sysops": {
      "command": "python3",
      "args": ["/absolute/path/to/sysops-mcp/server.py"]
    }
  }
}
```

Works with any MCP client that supports stdio transport.

## Design notes

A few deliberate decisions, since diagnostics tooling handed to an LLM deserves some care:

- **Read-only by design.** No kill, no restart, no writes. An assistant can diagnose, a human decides what to do about it. Every tool is annotated `readOnlyHint: true` so clients can reason about safety.
- **`tail_log` is sandboxed.** Reads are restricted to allowed roots (`/var/log` by default) with symlinks resolved *before* the check, so a confused or prompt-injected client can't use it as an arbitrary file reader (`/var/log/../../etc/passwd` is rejected).
- **Errors teach the next step.** A failed port check tells you whether the connection was refused (host up, nothing listening) or timed out (host down or firewalled), because those imply different follow-ups. A bad disk path suggests calling the tool without arguments to list valid mounts.
- **Context-efficient responses.** Markdown output is trimmed for an LLM's context window: rounded numbers, human-readable timestamps, no metadata dumps. Full fidelity is available via `response_format: "json"`.
- **Interpretation included.** `get_host_summary` doesn't just return numbers; it computes load-per-core and returns an assessment ("memory is nearly exhausted"), so the model spends its reasoning on the problem rather than the arithmetic.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

15 tests cover tool output shape, sorting/filtering, port-check outcomes (open, closed, DNS failure, via a real ephemeral listener), and the log path sandbox including traversal attempts.

## Roadmap

- `get_service_status` (systemd unit states)
- Docker container visibility (when a socket is present)
- Streamable HTTP transport for remote hosts

## License

MIT
