"""`openring mcp`: an MCP server over stdio, so an AI agent (Claude, Cursor, any MCP client) can place a phone call
through an OpenRing server and read how it went.

It speaks JSON-RPC 2.0, one message per line, with no dependency, and calls the OpenRing API at OPENRING_URL
(http://localhost:8000 by default) with OPENRING_API_KEY. Tools:

- `list_assistants`, `list_phone_numbers`: what can call, and from where.
- `place_call`: an assistant calls a phone number (OpenRing's POST /call); returns the call id.
- `get_call`: status, ended reason, duration and transcript of a call.
- `list_calls`: the last calls.

A call costs what the providers and the line charge; the agent places a real call to a real person, so MCP clients
should ask before using `place_call`.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

import httpx

from . import __version__, netguard

PROTOCOL = "2025-06-18"
TOOLS: list[dict[str, Any]] = [
    {"name": "list_assistants", "description": "List the voice assistants on this OpenRing server: id, name, greeting.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_phone_numbers", "description": "List the phone numbers this OpenRing server calls from.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "place_call",
     "description": "Have an assistant call a real phone number now. Costs money and reaches a real person: confirm "
                    "with the user first. Returns the call id; check it with get_call.",
     "inputSchema": {"type": "object", "required": ["phoneNumberId", "number"], "properties": {
         "phoneNumberId": {"type": "string", "description": "id from list_phone_numbers"},
         "number": {"type": "string", "description": "who to call, with country code, e.g. +5511912345678"},
         "assistantId": {"type": "string", "description": "id from list_assistants; the number's own if left out"},
         "name": {"type": "string", "description": "the person's name, for {{customer.name}} in the assistant"},
         "context": {"type": "string", "description": "what this call is about, as {{context}} in the assistant's "
                                                                      "prompt and greeting"}}}},
    {"name": "get_call", "description": "A call's status, ended reason, duration, cost and transcript.",
     "inputSchema": {"type": "object", "required": ["callId"], "properties": {"callId": {"type": "string"}}}},
    {"name": "list_calls", "description": "The last calls on this server, newest first.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}}},
]


class Api:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=url.rstrip("/"), timeout=30, verify=netguard.tls(),
                                             headers={"Authorization": f"Bearer {key}"})

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        r = self.client.request(method, path, json=body)
        data = r.json() if r.content else None
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise ValueError(f"OpenRing answered HTTP {r.status_code}: {detail or r.text[:200]}")
        return data


def run_tool(api: Api, name: str, args: dict[str, Any]) -> Any:
    if name == "list_assistants":
        return [{"id": a["id"], "name": a.get("name"), "firstMessage": a.get("firstMessage")}
                for a in api.call("GET", "/assistant")]
    if name == "list_phone_numbers":
        return [{"id": n["id"], "number": n.get("number"), "provider": n.get("provider", "twilio"),
                 "assistantId": n.get("assistantId")} for n in api.call("GET", "/phone-number")]
    if name == "place_call":
        customer = {"number": str(args["number"]), **({"name": str(args["name"])} if args.get("name") else {})}
        body: dict[str, Any] = {"phoneNumberId": str(args["phoneNumberId"]), "customer": customer}
        if args.get("assistantId"):
            body["assistantId"] = str(args["assistantId"])
        if args.get("context"):
            body["assistantOverrides"] = {"variableValues": {"context": str(args["context"])[:2000]}}
        call = api.call("POST", "/call", body)
        return {"callId": call["id"], "status": call.get("status")}
    if name == "get_call":
        c = api.call("GET", f"/call/{args['callId']}")
        return {"id": c["id"], "status": c.get("status"), "endedReason": c.get("endedReason"),
                "durationSeconds": c.get("durationSeconds"), "cost": c.get("cost"),
                "transcript": [{"role": m["role"], "message": m["message"]} for m in c.get("messages") or []
                               if m.get("role") in ("user", "assistant") and m.get("message")]}
    if name == "list_calls":
        limit = max(1, min(50, int(args.get("limit") or 10)))
        return [{"id": c["id"], "type": c.get("type"), "status": c.get("status"), "endedReason": c.get("endedReason"),
                 "customer": (c.get("customer") or {}).get("number"), "createdAt": c.get("createdAt")}
                for c in api.call("GET", f"/call?limit={limit}")]
    raise ValueError(f"unknown tool {name}")


def handle(api: Api, message: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC message in, its answer out (None for notifications)."""
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result: Any = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "openring", "version": __version__}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            out = run_tool(api, str(params.get("name")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": False}
        except (ValueError, KeyError, httpx.HTTPError) as exc:
            result = {"content": [{"type": "text", "text": f"{exc.__class__.__name__}: {exc}"}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(api: Api, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        answer = handle(api, message) if isinstance(message, dict) else None
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    key = os.environ.get("OPENRING_API_KEY", "").strip()
    if not key:
        sys.stderr.write("openring mcp: set OPENRING_API_KEY (Settings page of the dashboard)\n")
        raise SystemExit(2)
    serve(Api(os.environ.get("OPENRING_URL", "http://localhost:8000"), key))
