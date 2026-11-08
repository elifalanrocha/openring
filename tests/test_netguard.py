"""User-supplied URLs never reach cloud metadata or link-local addresses (SSRF), and stay out of logs."""

import asyncio
import socket

import httpx
import pytest

from openring import importer, netguard, server_messages, tools
from openring.netguard import UnsafeURL, blocked_address, check_url, safe_url_for_logs
from openring.providers import ProviderError, resolve_plan
from openring.store import Store

from test_api import ASSISTANT, AUTH, make_client

METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


def test_blocked_and_allowed_addresses():
    for bad in ("169.254.169.254", "169.254.0.1", "fd00:ec2::254", "fe80::1", "fe80::1%eth0",
                "::ffff:169.254.169.254", "100.100.100.200", "0.0.0.0", "224.0.0.1"):
        assert blocked_address(bad), bad
    # Local and private networks are normal homes for a tool server or a model.
    for ok in ("127.0.0.1", "::1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "8.8.8.8", "2001:4860:4860::8888"):
        assert not blocked_address(ok), ok


def test_check_url_refuses_metadata_in_every_spelling():
    for url in (METADATA, "http://[fd00:ec2::254]/latest", "http://169.254.169.254:80/"):
        with pytest.raises(UnsafeURL):
            check_url(url)
    # 169.254.169.254 written as one number: Linux resolves these (and the guard refuses them); Windows doesn't
    # resolve them at all, so no request can reach them either.
    for url in ("http://2852039166/latest", "http://0xA9FEA9FE/"):
        host = url.split("/")[2]
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            continue  # unreachable here
        with pytest.raises(UnsafeURL):
            check_url(url)
    for url in ("ftp://example.com/x", "file:///etc/passwd", "http:///nohost"):
        with pytest.raises(UnsafeURL):
            check_url(url)
    check_url("http://localhost:11434/v1")
    check_url("http://192.168.1.20:8080/tools")
    check_url("https://name-that-does-not-resolve.invalid/x")  # the request itself will fail normally


def test_a_hostname_that_resolves_to_metadata_is_refused(monkeypatch):
    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        if host == "metadata.google.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(UnsafeURL, match="169.254.169.254"):
        check_url("http://metadata.google.internal/computeMetadata/v1/")


def test_the_guarded_client_checks_every_request():
    sent = []

    def handle(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        client = httpx.AsyncClient(transport=netguard.GuardedTransport(httpx.MockTransport(handle)))
        ok = await client.get("http://127.0.0.1:9/ping")
        with pytest.raises(httpx.ConnectError, match="never calls"):
            await client.get(METADATA)
        await client.aclose()
        return ok

    assert asyncio.run(scenario()).status_code == 200
    assert sent == ["http://127.0.0.1:9/ping"]  # the metadata request never left


def test_import_refuses_a_metadata_source():
    with pytest.raises(importer.SourceError, match="never calls"):
        asyncio.run(importer.import_account("http://169.254.169.254", "key", Store(":memory:")))
    r = make_client().post("/import", json={"baseUrl": "http://169.254.169.254", "apiKey": "k"}, headers=AUTH)
    assert r.status_code == 400 and "never calls" in r.json()["detail"]


def test_a_tool_cannot_point_at_metadata_and_its_errors_never_show_the_url():
    tool = {"type": "function", "function": {"name": "book"}, "server": {"url": METADATA}}
    with pytest.raises(tools.ToolError, match="refused") as err:
        asyncio.run(tools.call_server(tool, "c1", {}, default_url=None))
    assert "/latest/meta-data" not in str(err.value)

    secret_hook = {"type": "function", "function": {"name": "book"},
                   "server": {"url": "https://hooks.example.com/catch/123/SECRET-TOKEN"}}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(tools.ToolError) as err:
        asyncio.run(tools.call_server(secret_hook, "c1", {}, default_url=None, client=client))
    # The model reads this text and could say it to the caller.
    assert "SECRET-TOKEN" not in str(err.value) and "HTTP 500" in str(err.value)


def test_server_messages_never_go_to_metadata(monkeypatch):
    calls = []

    async def scenario():
        await server_messages.send({"server": {"url": METADATA}}, "status-update", {"id": "c1"}, status="ended")
        with pytest.raises(server_messages.AssistantRequestError) as err:
            await server_messages.request_assistant({"url": METADATA}, {"id": "c1"}, {"number": "+15551234567"})
        return err.value

    monkeypatch.setattr(httpx.AsyncClient, "post", lambda *a, **k: calls.append(a))
    err = asyncio.run(scenario())
    assert calls == []  # refused before any request
    assert err.ended_reason == "assistant-request-failed" and "never calls" in str(err)


def test_a_failed_assistant_request_keeps_the_url_out_of_its_error():
    # The error is logged; webhook paths often carry a secret.
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    server = {"url": "https://hooks.example.com/catch/123/SECRET-TOKEN"}
    with pytest.raises(server_messages.AssistantRequestError) as err:
        asyncio.run(server_messages.request_assistant(server, {"id": "c1"}, {"number": "+15551234567"}, client=client))
    assert "SECRET-TOKEN" not in str(err.value) and "HTTP 503" in str(err.value)
    assert err.value.ended_reason == "assistant-request-failed"


def test_a_custom_llm_cannot_point_at_metadata():
    model = {"provider": "custom-llm", "model": "m", "url": "http://169.254.169.254/v1"}
    with pytest.raises(ProviderError, match="model.url"):
        resolve_plan({**ASSISTANT, "model": model}, check_keys=False)
    r = make_client().post("/assistant", json={**ASSISTANT, "model": model}, headers=AUTH)
    assert r.status_code == 400 and "never calls" in r.json()["detail"]
    # A fallback to such a model is dropped from the call's plan, with a warning: it is never called.
    plan = resolve_plan({**ASSISTANT, "model": {**ASSISTANT["model"], "fallbackModels": [model]}}, check_keys=False)
    assert "model" not in plan.fallbacks and any("never calls" in w for w in plan.warnings)
    ok = {"provider": "custom-llm", "model": "m", "url": "http://localhost:8080/v1"}
    assert make_client().post("/assistant", json={**ASSISTANT, "model": ok}, headers=AUTH).status_code == 201


def test_logs_get_the_scheme_and_host_only():
    assert safe_url_for_logs("https://hooks.zapier.com/hooks/catch/123/abc/?token=s3cret") == \
        "https://hooks.zapier.com/…"
    assert safe_url_for_logs("http://localhost:8080") == "http://localhost:8080"
    assert safe_url_for_logs("not a url") == "(invalid URL)"
