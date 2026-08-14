"""Tests for sysops_mcp tools.

Tools are exercised directly (they are plain async functions) rather than
through a full MCP client, which keeps the suite fast and dependency-light.
"""

import json

import pytest

from server import (
    CpuInput,
    DiskInput,
    HostInput,
    LogInput,
    MemoryInput,
    NetworkInput,
    PortCheckInput,
    ProcessInput,
    ResponseFormat,
    SortBy,
    check_port,
    get_cpu_status,
    get_disk_usage,
    get_host_summary,
    get_memory_status,
    get_network_status,
    list_top_processes,
    tail_log,
)


@pytest.mark.asyncio
async def test_cpu_status_json_shape():
    out = await get_cpu_status(CpuInput(sample_seconds=0.1, response_format=ResponseFormat.JSON))
    data = json.loads(out)
    assert 0 <= data["percent"] <= 100
    assert data["logical_cores"] >= 1
    assert set(data["load_avg"]) == {"1min", "5min", "15min"}


@pytest.mark.asyncio
async def test_cpu_per_core():
    out = await get_cpu_status(
        CpuInput(sample_seconds=0.1, per_core=True, response_format=ResponseFormat.JSON)
    )
    data = json.loads(out)
    assert isinstance(data["per_core_percent"], list)
    assert len(data["per_core_percent"]) == data["logical_cores"]


@pytest.mark.asyncio
async def test_memory_status():
    out = await get_memory_status(MemoryInput(response_format=ResponseFormat.JSON))
    data = json.loads(out)
    assert data["virtual"]["total_gb"] > 0
    assert 0 <= data["virtual"]["percent"] <= 100


@pytest.mark.asyncio
async def test_disk_usage_lists_partitions():
    out = await get_disk_usage(DiskInput(response_format=ResponseFormat.JSON))
    data = json.loads(out)
    assert len(data["partitions"]) >= 1
    root = [p for p in data["partitions"] if p["mountpoint"] == "/"]
    assert root, "expected root partition in results"


@pytest.mark.asyncio
async def test_disk_usage_bad_path_is_actionable():
    out = await get_disk_usage(DiskInput(path="/definitely/not/a/real/path"))
    assert out.startswith("Error:")
    assert "get_disk_usage" in out  # suggests the fix


@pytest.mark.asyncio
async def test_top_processes_sorted_and_limited():
    out = await list_top_processes(
        ProcessInput(sort_by=SortBy.MEMORY, limit=5, response_format=ResponseFormat.JSON)
    )
    data = json.loads(out)
    assert data["count"] <= 5
    mems = [p["memory_percent"] for p in data["processes"]]
    assert mems == sorted(mems, reverse=True)


@pytest.mark.asyncio
async def test_process_name_filter():
    out = await list_top_processes(
        ProcessInput(name_filter="python", limit=50, response_format=ResponseFormat.JSON)
    )
    data = json.loads(out)
    for p in data["processes"]:
        assert "python" in p["name"].lower()


@pytest.mark.asyncio
async def test_network_status():
    out = await get_network_status(NetworkInput(response_format=ResponseFormat.JSON))
    data = json.loads(out)
    assert "lo" in data["interfaces"] or len(data["interfaces"]) >= 1
    assert data["io_totals"]["recv_gb"] >= 0


@pytest.mark.asyncio
async def test_check_port_closed():
    # Port 1 on localhost should be refused or filtered, never open.
    out = await check_port(PortCheckInput(host="127.0.0.1", port=1, timeout_seconds=1.0))
    assert "OPEN" not in out


@pytest.mark.asyncio
async def test_check_port_open(unused_tcp_listener):
    host, port = unused_tcp_listener
    out = await check_port(PortCheckInput(host=host, port=port, timeout_seconds=2.0))
    assert "OPEN" in out


@pytest.mark.asyncio
async def test_check_port_bad_dns():
    out = await check_port(
        PortCheckInput(host="this-host-does-not-exist.invalid", port=80, timeout_seconds=1.0)
    )
    assert "could not resolve" in out


@pytest.mark.asyncio
async def test_host_summary():
    out = await get_host_summary(HostInput(response_format=ResponseFormat.JSON))
    data = json.loads(out)
    assert data["uptime_hours"] >= 0
    assert data["assessment"]


@pytest.mark.asyncio
async def test_tail_log_blocks_paths_outside_allowed_roots():
    out = await tail_log(LogInput(path="/etc/passwd", lines=5))
    assert out.startswith("Error:")
    assert "allowed log roots" in out


@pytest.mark.asyncio
async def test_tail_log_blocks_traversal():
    out = await tail_log(LogInput(path="/var/log/../../etc/passwd", lines=5))
    assert out.startswith("Error:")


@pytest.mark.asyncio
async def test_tail_log_missing_file():
    out = await tail_log(LogInput(path="/var/log/no-such-file-xyz.log", lines=5))
    assert "does not exist" in out
