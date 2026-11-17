"""Vapi's `mcp` tool: an assistant uses the tools of an MCP server (a Model Context Protocol server, over HTTP).

The tool names a server (`server.url`, `server.headers`); at the start of a call OpenRing asks it which tools it has
(`tools/list`) and gives each to the model as a function of its own; when the model calls one, `tools/call` goes to the
server and its answer is read back. Only the Streamable HTTP transport is spoken (one POST per message, answered as JSON
or as a short event stream). A server that only offers the older two-endpoint SSE transport gives no tools, and the
call goes on without them.

The server's address comes from outside, so it goes through `netguard` like every address a person typed. Its answers
are capped, nothing is followed or retried, and errors never carry the address or the headers, which often hold a key.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from loguru import logger

from . import netguard

PROTOCOL = "2025-03-26"
MAX_BYTES = 1_000_000  # of one answer
MAX_TOOLS = 50  # what a model is given: every tool costs tokens, on every turn
MAX_TEXT = 4_000  # of a tool's result that the model reads
DISCOVERY_SECONDS = 8.0  # a call doesn't wait longer than this for a server's tool list
CALL_SECONDS = 20.0
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class McpError(Exception):
    """The server refused, or couldn't be reached. The text is safe to show the model."""


def function_name(name: str) -> str:
    """A tool's name as the model's API takes it (letters, digits, `_` and `-`, at most 64)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "tool"


def _headers(server: dict[str, Any], session: str | None = None) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": PROTOCOL})
    if session:
        headers["Mcp-Session-Id"] = session
    return headers


def _messages(body: str, content_type: str) -> list[dict[str, Any]]:
    """The JSON-RPC messages an answer holds: one JSON document, or the `data:` lines of an event stream."""
    texts = [body] if "text/event-stream" not in content_type else [
        line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    found = []
    for text in texts:
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        found += [m for m in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(m, dict)]
    return found


class Connection:
    """One conversation with an MCP server: initialize, then requests, all with the same session."""

    def __init__(self, server: dict[str, Any], *, client: httpx.AsyncClient | None = None):
        self.server = server
        self.url = str(server.get("url") or "")
        self.session: str | None = None
        self._next = 0
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> Connection:
        try:
            await netguard.check_url_async(self.url)
        except netguard.UnsafeURL:
            raise McpError("the MCP server's address is refused: it points to a cloud metadata or link-local "
                           "address") from None
        if self._client is None:
            self._client = netguard.client()
        await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "openring", "version": "1"}})
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()

    async def _post(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        received = bytearray()
        try:
            assert self._client is not None
            async with self._client.stream("POST", self.url, json=message, timeout=CALL_SECONDS,
                                           headers=_headers(self.server, self.session)) as r:
                if r.status_code == 202 or (r.status_code == 200 and "id" not in message):
                    return []  # a notification: nothing to read
                if not 200 <= r.status_code < 300:  # a redirect is not followed: the guard wouldn't have seen it
                    raise McpError(f"the MCP server answered HTTP {r.status_code}")
                if r.headers.get("Mcp-Session-Id"):
                    self.session = r.headers["Mcp-Session-Id"]
                async for chunk in r.aiter_bytes():
                    received += chunk
                    if len(received) > MAX_BYTES:
                        raise McpError("the MCP server's answer is too large")
                content_type = r.headers.get("content-type", "")
        except McpError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise McpError(f"the MCP server could not be reached ({exc.__class__.__name__})") from None
        return _messages(bytes(received).decode("utf-8", "replace"), content_type)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next += 1
        wanted = self._next
        answers = await self._post({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params or {}})
        reply = next((m for m in answers if m.get("id") == wanted), None)
        if reply is None:
            raise McpError(f"the MCP server gave no answer to {method}")
        if isinstance(reply.get("error"), dict):
            raise McpError(f"the MCP server refused {method}: {str(reply['error'].get('message'))[:200]}")
        return reply.get("result") or {}


async def list_tools(server: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """The tools an MCP server offers: `{name, description, inputSchema}`, at most `MAX_TOOLS`."""
    found: list[dict[str, Any]] = []
    async with asyncio.timeout(DISCOVERY_SECONDS):
        async with Connection(server, client=client) as conn:
            cursor = None
            while len(found) < MAX_TOOLS:
                page = await conn.request("tools/list", {"cursor": cursor} if cursor else {})
                found += [t for t in page.get("tools") or [] if isinstance(t, dict) and t.get("name")]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
    return found[:MAX_TOOLS]


async def call_tool(server: dict[str, Any], name: str, arguments: dict[str, Any], *, timeout: float = CALL_SECONDS,
                    client: httpx.AsyncClient | None = None) -> str:
    """Run one tool of the server and return what the model should read."""
    try:
        async with asyncio.timeout(timeout):
            async with Connection(server, client=client) as conn:
                result = await conn.request("tools/call", {"name": name, "arguments": arguments})
    except TimeoutError:
        raise McpError(f"no answer after {timeout:g} s, so it may or may not have gone through") from None
    parts = [str(c.get("text")) for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    if not parts and result.get("structuredContent") is not None:
        parts = [json.dumps(result["structuredContent"], ensure_ascii=False)]
    text = "\n".join(parts).strip() or "Done."
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + " [answer cut short]"
    return f"The tool reported an error: {text}" if result.get("isError") else text


async def expand(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace each `mcp` tool with the functions its server offers (`mcpTool`), so the model can pick one.

    A server that can't be reached costs its tools, not the call: the call goes on without them.
    """
    out: list[dict[str, Any]] = []
    taken = {str((t.get("function") or {}).get("name")) for t in tools if t.get("type") != "mcp"}
    for tool in tools:
        if tool.get("type") != "mcp":
            out.append(tool)
            continue
        server = tool.get("server") or {}
        try:
            offered = await list_tools(server)
        except (McpError, TimeoutError) as exc:
            logger.warning("the MCP server of tool '{}' gave no tools; the call runs without them: {}",
                           (tool.get("function") or {}).get("name") or "mcp", exc or "it took too long")
            continue
        for item in offered:
            name = function_name(str(item["name"]))
            if name in taken or not NAME.fullmatch(name):
                continue  # the model needs unique names
            taken.add(name)
            schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
            out.append({"type": "mcpTool", "mcpName": str(item["name"]), "server": server,
                        "timeoutSeconds": tool.get("timeoutSeconds") or server.get("timeoutSeconds"),
                        "function": {"name": name, "description": str(item.get("description") or ""),
                                     "parameters": {"type": "object", "properties": schema.get("properties") or {},
                                                    "required": list(schema.get("required") or [])}},
                        "messages": tool.get("messages") or []})
    return out
