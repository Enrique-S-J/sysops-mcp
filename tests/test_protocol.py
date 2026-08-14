"""Tests for the MCP layer itself, not the diagnostics logic.

test_server.py calls the tool functions directly, which says nothing about
what the server actually advertises or accepts over the protocol. These tests
drive the real MCPServer: schema generation, argument unmarshalling, and the
tool metadata a client sees.

The gap these close is not hypothetical. Every tool once took a single
Pydantic model parameter, which advertised each one as accepting a lone
opaque object; the direct-call suite passed while no ordinary client could
call the tools with the argument shape their schemas implied.
"""

import pytest

from server import ALLOWED_LOG_ROOTS, mcp

EXPECTED_TOOLS = {
    "get_cpu_status": {"sample_seconds", "per_core", "response_format"},
    "get_memory_status": {"response_format"},
    "get_disk_usage": {"path", "warn_percent", "response_format"},
    "list_top_processes": {"sort_by", "limit", "name_filter", "response_format"},
    "get_network_status": {"include_connections", "response_format"},
    "check_port": {"port", "host", "timeout_seconds"},
    "get_host_summary": {"response_format"},
    "tail_log": {"path", "lines", "grep"},
}


async def _tools_by_name():
    return {t.name: t for t in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_all_tools_are_advertised():
    assert set((await _tools_by_name())) == set(EXPECTED_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,expected_params", sorted(EXPECTED_TOOLS.items()))
async def test_parameters_are_named_not_wrapped(name, expected_params):
    """Each parameter is its own top-level schema property.

    A single model parameter would collapse these into one property named
    'params' pointing at a $ref, which is the regression this guards.
    """
    schema = (await _tools_by_name())[name].input_schema
    assert set(schema["properties"]) == expected_params
    assert "params" not in schema["properties"]


@pytest.mark.asyncio
async def test_descriptions_are_on_the_arguments():
    """Every argument carries its own description, not just the wrapper."""
    tools = await _tools_by_name()
    for name, expected_params in EXPECTED_TOOLS.items():
        props = tools[name].input_schema["properties"]
        for param in expected_params:
            assert props[param].get("description"), f"{name}.{param} has no description"


@pytest.mark.asyncio
async def test_required_arguments():
    tools = await _tools_by_name()
    assert tools["tail_log"].input_schema["required"] == ["path"]
    assert tools["check_port"].input_schema["required"] == ["port"]
    # Tools that are safe to call with no arguments must not require any.
    for name in ("get_host_summary", "get_cpu_status", "get_memory_status"):
        assert not tools[name].input_schema.get("required")


@pytest.mark.asyncio
async def test_every_tool_is_marked_read_only():
    """Clients rely on readOnlyHint to skip approval prompts.

    Asserted on the serialized form: the SDK exposes these as snake_case in
    Python but sends them camelCase, and the camelCase spelling is what a
    client sees.
    """
    for name, tool in (await _tools_by_name()).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        wire = tool.annotations.model_dump(by_alias=True)
        assert wire["readOnlyHint"] is True, f"{name} is not read-only"
        assert wire["destructiveHint"] is False, f"{name} is destructive"


@pytest.mark.asyncio
async def test_call_tool_with_flat_arguments():
    result = await mcp.call_tool("get_host_summary", {})
    assert not result.is_error
    assert "Host Summary" in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_argument_bounds_are_enforced():
    with pytest.raises(Exception) as exc:
        await mcp.call_tool("tail_log", {"path": "/var/log/x", "lines": 10_000})
    assert "less than or equal to" in str(exc.value)


@pytest.mark.asyncio
async def test_log_sandbox_enforced_through_the_protocol():
    """The path check must hold on the real call path, not just in-process."""
    result = await mcp.call_tool("tail_log", {"path": "/etc/passwd", "lines": 3})
    assert "allowed log roots" in result.content[0].text


@pytest.mark.asyncio
async def test_log_path_whitespace_is_stripped():
    """Padding must not turn an allowed absolute path into a relative one."""
    root = ALLOWED_LOG_ROOTS[0]
    result = await mcp.call_tool(
        "tail_log", {"path": f"  {root}/no-such-file-xyz.log  ", "lines": 3}
    )
    assert "does not exist" in result.content[0].text
