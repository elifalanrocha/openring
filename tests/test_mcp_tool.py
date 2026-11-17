"""Vapi's `mcp` tool: an assistant uses the tools of an MCP server. The server is a stub served through httpx."""

import asyncio
import json

import httpx
import pytest

from openring import mcpclient, tools
from openring.store import Store
from openring.tools import ToolError, assistant_tools, check_tool, function_schema

URL = "https://mcp.example.com/mcp"
TOOL = {"type": "mcp", "function": {"name": "mcpTools"}, "metadata": {"protocol": "shttp"},
        "server": {"url": URL, "headers": {"Authorization": "Bearer s3cret"}}}
OFFERED = [
    {"name": "get_weather", "description": "Weather in a city",
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "search.docs", "description": "Search",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
]


def server(handle=None, seen=None, as_stream=False):
    """A Streamable HTTP MCP server: initialize, tools/list, tools/call."""
    def answer(request):
        message = json.loads(request.content)
        if seen is not None:
            seen.append((message, dict(request.headers)))
        if "id" not in message:
            return httpx.Response(202)
        method, params = message["method"], message.get("params") or {}
        if handle and (custom := handle(method, params)) is not None:
            result = custom
        elif method == "initialize":
            result = {"protocolVersion": mcpclient.PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "x"}}
        elif method == "tools/list":
            result = {"tools": OFFERED}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": f"Sunny in {params['arguments'].get('city')}"}]}
        else:
            refusal = {"code": -32601, "message": "no"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "error": refusal})
        body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        headers = {"Mcp-Session-Id": "s-1"}
        if as_stream:
            return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"},
                                  text=f"event: message\ndata: {json.dumps(body)}\n\n")
        return httpx.Response(200, json=body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


def run(coroutine_of):
    async def go():
        async with coroutine_of[0] as client:
            return await coroutine_of[1](client)

    return asyncio.run(go())


def test_the_servers_tools_are_listed_with_its_headers_and_session():
    seen = []
    got = run((server(seen=seen), lambda c: mcpclient.list_tools(TOOL["server"], client=c)))
    assert [t["name"] for t in got] == ["get_weather", "search.docs"]
    assert [m["method"] for m, _ in seen] == ["initialize", "notifications/initialized", "tools/list"]
    assert all(h["authorization"] == "Bearer s3cret" for _, h in seen)  # the server's own key goes with every message
    assert seen[-1][1]["mcp-session-id"] == "s-1"  # the session the server gave, as asked


def test_an_answer_given_as_an_event_stream_is_read_too():
    got = run((server(as_stream=True), lambda c: mcpclient.list_tools(TOOL["server"], client=c)))
    assert len(got) == 2


def test_a_tool_is_called_with_the_models_arguments_and_its_text_comes_back():
    seen = []
    said = run((server(seen=seen), lambda c: mcpclient.call_tool(TOOL["server"], "get_weather", {"city": "Lisbon"},
                                                                  client=c)))
    assert said == "Sunny in Lisbon"
    assert seen[-1][0]["params"] == {"name": "get_weather", "arguments": {"city": "Lisbon"}}


def test_a_tool_that_fails_or_answers_too_much_tells_the_model_plainly():
    def failing(method, params):
        if method == "tools/call":
            return {"isError": True, "content": [{"type": "text", "text": "city not found"}]}

    said = run((server(failing), lambda c: mcpclient.call_tool(TOOL["server"], "get_weather", {}, client=c)))
    assert said == "The tool reported an error: city not found"

    def long(method, params):
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "x" * 10_000}]}

    said = run((server(long), lambda c: mcpclient.call_tool(TOOL["server"], "t", {}, client=c)))
    assert len(said) < 4_100 and said.endswith("[answer cut short]")


def test_errors_never_carry_the_address_or_the_headers():
    def broken(request):
        raise httpx.ConnectError("boom https://mcp.example.com/mcp Bearer s3cret")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            await mcpclient.list_tools(TOOL["server"], client=client)

    with pytest.raises(mcpclient.McpError) as raised:
        asyncio.run(go())
    assert "s3cret" not in str(raised.value) and "mcp.example.com" not in str(raised.value)

    redirect = httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    refused = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: redirect))
    with pytest.raises(mcpclient.McpError, match="HTTP 302"):  # a redirect is not followed
        asyncio.run(mcpclient.list_tools(TOOL["server"], client=refused))
    with pytest.raises(mcpclient.McpError, match="refused"):  # nor is a cloud's metadata address ever asked
        asyncio.run(mcpclient.list_tools({"url": "http://169.254.169.254/mcp"}))


def test_each_tool_of_the_server_becomes_a_function_for_the_model(monkeypatch):
    async def fake(server_, **_):
        return OFFERED

    monkeypatch.setattr(mcpclient, "list_tools", fake)
    own = {"type": "function", "function": {"name": "get_weather", "parameters": {}},
           "server": {"url": "https://x.example.com"}}
    expanded = asyncio.run(mcpclient.expand([own, TOOL]))
    names = [tools.tool_name(t) for t in expanded]
    # the server's get_weather loses to the assistant's own, and the "." of search.docs becomes "_"
    assert names == ["get_weather", "search_docs"]
    mine = expanded[1]
    assert mine["type"] == "mcpTool" and mine["mcpName"] == "search.docs"
    assert function_schema(mine) == {"name": "search_docs", "description": "Search",
                                     "properties": {"q": {"type": "string"}}, "required": []}


def test_a_server_that_cannot_be_reached_costs_its_tools_not_the_call(monkeypatch):
    async def down(server_, **_):
        raise mcpclient.McpError("the MCP server could not be reached (ConnectError)")

    monkeypatch.setattr(mcpclient, "list_tools", down)
    other = {"type": "endCall"}
    assert asyncio.run(mcpclient.expand([TOOL, other])) == [other]


def test_an_mcp_tool_is_checked_and_survives_saving_and_listing(monkeypatch):
    check_tool(TOOL)
    for bad in ({**TOOL, "server": {}}, {**TOOL, "server": {"url": "ftp://x"}},
                {**TOOL, "server": {"url": URL, "headers": []}}):
        with pytest.raises(ToolError):
            check_tool(bad)
    store = Store(":memory:")
    kept = [t for t in assistant_tools({"model": {"tools": [TOOL, {"type": "dtmf"}]}}, store)]
    assert kept == [TOOL]  # supported now: dtmf is still skipped
    assert tools.register_tools(object(), [TOOL], controls=None) is None  # never given to a model unexpanded
    assert tools.tool_name(TOOL) == "mcpTools"
    assert tools.tool_name({**TOOL, "function": {}}) == "mcp:" + URL
