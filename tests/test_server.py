"""Tests for sysops_mcp tool logic.

Tools are exercised directly (they are plain async functions) rather than
through a full MCP client, which keeps the suite fast and dependency-light.
See test_protocol.py for coverage of the MCP layer itself.
"""

import json
from types import SimpleNamespace

import pytest

import server
from server import (
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


def _vm(percent: float) -> SimpleNamespace:
    """Minimal stand-in for psutil.virtual_memory()."""
    total = 16 * 1024**3
    used = int(total * percent / 100)
    return SimpleNamespace(
        total=total, used=used, available=total - used, percent=percent
    )


def _swap(total: int, percent: float) -> SimpleNamespace:
    """Minimal stand-in for psutil.swap_memory()."""
    return SimpleNamespace(total=total, used=int(total * percent / 100), percent=percent)


def _fake_partitions(monkeypatch, specs) -> None:
    """Patch disk_partitions/disk_usage from (mount, device, fstype, opts, pct)."""
    parts = [
        SimpleNamespace(mountpoint=m, device=d, fstype=f, opts=o)
        for m, d, f, o, _ in specs
    ]
    percents = {m: p for m, _, _, _, p in specs}
    total = 100 * 1024**3

    monkeypatch.setattr(server.psutil, "disk_partitions", lambda all=False: parts)
    monkeypatch.setattr(
        server.psutil,
        "disk_usage",
        lambda mount: SimpleNamespace(
            total=total,
            used=int(total * percents[mount] / 100),
            free=total - int(total * percents[mount] / 100),
            percent=percents[mount],
        ),
    )


@pytest.mark.asyncio
async def test_cpu_status_json_shape():
    out = await get_cpu_status(sample_seconds=0.1, response_format=ResponseFormat.JSON)
    data = json.loads(out)
    assert 0 <= data["percent"] <= 100
    assert data["logical_cores"] >= 1
    assert set(data["load_avg"]) == {"1min", "5min", "15min"}


@pytest.mark.asyncio
async def test_cpu_per_core():
    out = await get_cpu_status(
        sample_seconds=0.1, per_core=True, response_format=ResponseFormat.JSON
    )
    data = json.loads(out)
    assert isinstance(data["per_core_percent"], list)
    assert len(data["per_core_percent"]) == data["logical_cores"]


@pytest.mark.asyncio
async def test_memory_status():
    out = await get_memory_status(response_format=ResponseFormat.JSON)
    data = json.loads(out)
    assert data["virtual"]["total_gb"] > 0
    assert 0 <= data["virtual"]["percent"] <= 100


@pytest.mark.asyncio
async def test_disk_usage_lists_partitions():
    out = await get_disk_usage(response_format=ResponseFormat.JSON)
    data = json.loads(out)
    assert len(data["partitions"]) >= 1
    root = [p for p in data["partitions"] if p["mountpoint"] == "/"]
    assert root, "expected root partition in results"


@pytest.mark.asyncio
async def test_disk_usage_bad_path_is_actionable():
    out = await get_disk_usage(path="/definitely/not/a/real/path")
    assert out.startswith("Error:")
    assert "get_disk_usage" in out  # suggests the fix


@pytest.mark.asyncio
async def test_disk_usage_does_not_warn_on_read_only_mounts(monkeypatch):
    """A full squashfs snap mount is reported but not flagged."""
    _fake_partitions(
        monkeypatch,
        [("/", "/dev/sda1", "ext4", "rw,relatime", 55.0),
         ("/snap/core20/2015", "/dev/loop0", "squashfs", "ro,nodev,relatime", 100.0)],
    )
    data = json.loads(await get_disk_usage(response_format=ResponseFormat.JSON))
    by_mount = {p["mountpoint"]: p for p in data["partitions"]}
    assert by_mount["/snap/core20/2015"]["percent"] == 100.0
    assert by_mount["/snap/core20/2015"]["read_only"] is True
    assert by_mount["/snap/core20/2015"]["warning"] is False
    assert data["warnings"] == 0


@pytest.mark.asyncio
async def test_top_processes_sorted_and_limited():
    out = await list_top_processes(
        sort_by=SortBy.MEMORY, limit=5, response_format=ResponseFormat.JSON
    )
    data = json.loads(out)
    assert data["count"] <= 5
    mems = [p["memory_percent"] for p in data["processes"]]
    assert mems == sorted(mems, reverse=True)


@pytest.mark.asyncio
async def test_process_name_filter():
    out = await list_top_processes(
        name_filter="python", limit=50, response_format=ResponseFormat.JSON
    )
    data = json.loads(out)
    for p in data["processes"]:
        assert "python" in p["name"].lower()


@pytest.mark.asyncio
async def test_network_status():
    out = await get_network_status(response_format=ResponseFormat.JSON)
    data = json.loads(out)
    assert "lo" in data["interfaces"] or len(data["interfaces"]) >= 1
    assert data["io_totals"]["recv_gb"] >= 0


@pytest.mark.asyncio
async def test_check_port_closed():
    # Port 1 on localhost should be refused or filtered, never open.
    out = await check_port(host="127.0.0.1", port=1, timeout_seconds=1.0)
    assert "OPEN" not in out


@pytest.mark.asyncio
async def test_check_port_open(unused_tcp_listener):
    host, port = unused_tcp_listener
    out = await check_port(host=host, port=port, timeout_seconds=2.0)
    assert "OPEN" in out


@pytest.mark.asyncio
async def test_check_port_bad_dns():
    out = await check_port(
        host="this-host-does-not-exist.invalid", port=80, timeout_seconds=1.0
    )
    assert "could not resolve" in out


@pytest.mark.asyncio
async def test_host_summary():
    out = await get_host_summary(response_format=ResponseFormat.JSON)
    data = json.loads(out)
    assert data["uptime_hours"] >= 0
    assert data["assessment"]
    assert 0 <= data["swap_percent"] <= 100


@pytest.mark.asyncio
async def test_host_summary_flags_swap_pressure(monkeypatch):
    """Swapping is reported even when RAM and CPU look healthy."""
    monkeypatch.setattr(server.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(
        server.psutil, "virtual_memory", lambda: _vm(percent=60.0)
    )
    monkeypatch.setattr(
        server.psutil, "swap_memory", lambda: _swap(total=4 * 1024**3, percent=75.0)
    )
    data = json.loads(await get_host_summary(response_format=ResponseFormat.JSON))
    assert "swapping" in data["assessment"]
    assert data["swap_percent"] == 75.0


@pytest.mark.asyncio
async def test_host_summary_prefers_exhaustion_over_swapping(monkeypatch):
    """Exhausted RAM is the more actionable finding, so it wins."""
    monkeypatch.setattr(server.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(
        server.psutil, "virtual_memory", lambda: _vm(percent=97.0)
    )
    monkeypatch.setattr(
        server.psutil, "swap_memory", lambda: _swap(total=4 * 1024**3, percent=99.0)
    )
    data = json.loads(await get_host_summary(response_format=ResponseFormat.JSON))
    assert "nearly exhausted" in data["assessment"]
    assert "swapping" not in data["assessment"]


@pytest.mark.asyncio
async def test_host_summary_ignores_full_read_only_mounts(monkeypatch):
    """A permanently-full snap mount must not pin the assessment on.

    Every Ubuntu host with snaps installed carries squashfs loop mounts at
    100%, which would otherwise report "a disk is nearly full" forever.
    """
    monkeypatch.setattr(server.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(server.psutil, "virtual_memory", lambda: _vm(percent=40.0))
    monkeypatch.setattr(server.psutil, "swap_memory", lambda: _swap(total=0, percent=0.0))
    _fake_partitions(
        monkeypatch,
        [("/", "/dev/sda1", "ext4", "rw,relatime", 45.0),
         ("/snap/core20/2015", "/dev/loop0", "squashfs", "ro,nodev,relatime", 100.0)],
    )
    data = json.loads(await get_host_summary(response_format=ResponseFormat.JSON))
    assert data["max_disk_percent"] == 45.0
    assert data["assessment"] == "no immediate resource pressure detected"


@pytest.mark.asyncio
async def test_host_summary_still_flags_writable_full_disks(monkeypatch):
    """The read-only exclusion must not mask a genuinely full writable disk."""
    monkeypatch.setattr(server.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(server.psutil, "virtual_memory", lambda: _vm(percent=40.0))
    monkeypatch.setattr(server.psutil, "swap_memory", lambda: _swap(total=0, percent=0.0))
    _fake_partitions(
        monkeypatch,
        [("/", "/dev/sda1", "ext4", "rw,relatime", 96.0),
         ("/snap/core20/2015", "/dev/loop0", "squashfs", "ro,nodev,relatime", 100.0)],
    )
    data = json.loads(await get_host_summary(response_format=ResponseFormat.JSON))
    assert data["max_disk_percent"] == 96.0
    assert "a disk is nearly full" in data["assessment"]


@pytest.mark.asyncio
async def test_host_summary_ignores_swap_on_swapless_hosts(monkeypatch):
    """A host with swap disabled reports 0/0 and must not be flagged."""
    monkeypatch.setattr(server.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(
        server.psutil, "virtual_memory", lambda: _vm(percent=40.0)
    )
    monkeypatch.setattr(
        server.psutil, "swap_memory", lambda: _swap(total=0, percent=0.0)
    )
    data = json.loads(await get_host_summary(response_format=ResponseFormat.JSON))
    assert data["assessment"] == "no immediate resource pressure detected"


@pytest.mark.asyncio
async def test_tail_log_blocks_paths_outside_allowed_roots():
    out = await tail_log(path="/etc/passwd", lines=5)
    assert out.startswith("Error:")
    assert "allowed log roots" in out


@pytest.mark.asyncio
async def test_tail_log_blocks_traversal():
    out = await tail_log(path="/var/log/../../etc/passwd", lines=5)
    assert out.startswith("Error:")
    assert "allowed log roots" in out


@pytest.mark.asyncio
async def test_tail_log_missing_file():
    out = await tail_log(path="/var/log/no-such-file-xyz.log", lines=5)
    assert "does not exist" in out
