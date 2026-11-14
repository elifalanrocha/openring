"""`openring mcp`: the MCP server an AI agent uses to place calls through OpenRing, driven over its stdio protocol."""

import io
import json

import pytest

from openring import mcp, twilio

from test_api import ASSISTANT, AUTH, make_client


@pytest.fixture
def agent(monkeypatch):
    """An OpenRing with an assistant and a Twilio number, and an MCP server that talks to it."""
    async def create_call(account_sid, auth_token, *, to, from_, url, status_callback, machine_detection=None):
        return "CA-mcp-1"

    monkeypatch.setattr(twilio, "create_call", create_call)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    client = make_client(twilio_account_sid="AC1")
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH).json()
    client.headers.update(AUTH)  # the MCP server holds the API key
    return mcp.Api("", "", client=client), a, n


def talk(api, *messages):
    out = io.StringIO()
    mcp.serve(api, io.StringIO("".join(json.dumps(m) + "\n" for m in messages) + "not json\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def tool(api, tool_name, **arguments):
    answer = talk(api, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments}})[0]["result"]
    return answer["isError"], (answer["content"][0]["text"] if answer["isError"]
                               else json.loads(answer["content"][0]["text"]))


def test_an_agent_learns_the_tools(agent):
    api, *_ = agent
    init, listed, pong, unknown, bad = talk(
        api, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # a notification: no answer
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert init["result"]["serverInfo"]["name"] == "openring" and "tools" in init["result"]["capabilities"]
    names = [t["name"] for t in listed["result"]["tools"]]
    assert names == ["list_assistants", "list_phone_numbers", "place_call", "get_call", "list_calls"]
    assert "confirm" in next(t for t in listed["result"]["tools"] if t["name"] == "place_call")["description"]
    assert pong["result"] == {} and unknown["error"]["code"] == -32601 and bad["error"]["code"] == -32700


def test_an_agent_places_a_call_and_reads_it(agent):
    api, a, n = agent
    error, assistants = tool(api, "list_assistants")
    assert not error and assistants[0]["id"] == a["id"]
    error, numbers = tool(api, "list_phone_numbers")
    assert numbers == [{"id": n["id"], "number": "+15551234567", "provider": "twilio", "assistantId": a["id"]}]
    error, placed = tool(api, "place_call", phoneNumberId=n["id"], number="+5511912345678", name="Ana",
                         context="Confirm Friday's 3 pm appointment")
    assert not error and placed["status"] == "queued"
    error, call = tool(api, "get_call", callId=placed["callId"])
    assert call["status"] == "queued" and call["transcript"] == []
    stored = api.client.get(f"/call/{placed['callId']}").json()
    assert stored["customer"] == {"number": "+5511912345678", "name": "Ana"}
    assert stored["assistantOverrides"]["variableValues"]["context"] == "Confirm Friday's 3 pm appointment"
    error, calls = tool(api, "list_calls", limit=5)
    assert calls[0]["id"] == placed["callId"]


def test_mistakes_come_back_as_tool_errors_not_crashes(agent):
    api, _a, n = agent
    error, text = tool(api, "place_call", phoneNumberId=n["id"], number="not a number")
    assert error and "HTTP 400" in text
    error, text = tool(api, "get_call", callId="nobody")
    assert error and "HTTP 404" in text
    error, text = tool(api, "place_call", number="+5511912345678")  # phoneNumberId missing
    assert error and "KeyError" in text
    error, text = tool(api, "delete_everything")
    assert error and "unknown tool" in text


def test_it_needs_the_api_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENRING_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        mcp.main()
    assert "OPENRING_API_KEY" in capsys.readouterr().err
