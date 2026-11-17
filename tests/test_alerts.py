"""Alerts: a failed call is reported once to the address you set, and only then."""

import asyncio

import httpx
import pytest

from openring import alerts
from openring.store import Store

from test_api import AUTH, make_client

URL = "https://alerts.example.com/hook"


def call(store, ended_at, reason, **fields):
    return store.create("calls", {"status": "ended", "endedAt": ended_at, "endedReason": reason, "assistantId": "a",
                                  "customer": {"number": "+15550000001"},
                                  "artifact": {"messages": [{"message": "secret"}]}, **fields})


def run_tell(store):
    sent = []

    def answer(request):
        sent.append(request)
        return httpx.Response(200)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await alerts.tell(store, client)

    return asyncio.run(go()), sent


def test_a_failed_call_is_told_once_without_the_callers_number_or_words():
    store = Store(":memory:")
    store.set_setting(alerts.KEY, URL)
    call(store, "2026-11-17T10:00:00Z", "pipeline-error: the model stopped working: no credit")
    call(store, "2026-11-17T10:01:00Z", "customer-ended-call")  # a normal ending is no alert
    n, sent = run_tell(store)
    assert n == 1 and str(sent[0].url) == URL
    body = sent[0].content.decode()
    assert '"call-failed"' in body and "pipeline-error" in body
    assert "+1555" not in body and "secret" not in body
    assert run_tell(store)[0] == 0  # told already: a second round says nothing
    call(store, "2026-11-17T10:05:00Z", "twilio-failed-to-connect-call")
    assert run_tell(store)[0] == 1


def test_without_an_address_failures_are_remembered_so_turning_it_on_does_not_flood():
    store = Store(":memory:")
    call(store, "2026-11-17T10:00:00Z", "pipeline-error: x")
    assert run_tell(store) == (0, [])
    store.set_setting(alerts.KEY, URL)
    assert run_tell(store)[0] == 0
    call(store, "2026-11-17T11:00:00Z", "pipeline-error: y")
    assert run_tell(store)[0] == 1


def test_an_address_that_is_down_does_not_stop_the_watcher():
    store = Store(":memory:")
    store.set_setting(alerts.KEY, URL)
    call(store, "2026-11-17T10:00:00Z", "pipeline-error: x")

    def broken(request):
        raise httpx.ConnectError("down")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            return await alerts.tell(store, client)

    assert asyncio.run(go()) == 0


def test_the_address_is_set_through_the_api_and_refused_when_unsafe():
    client = make_client()
    assert client.get("/settings/alerts").status_code == 401
    assert client.get("/settings/alerts", headers=AUTH).json() == {"alertWebhook": None}
    ok = client.patch("/settings/alerts", json={"alertWebhook": URL}, headers=AUTH)
    assert ok.status_code == 200 and client.get("/settings/alerts", headers=AUTH).json()["alertWebhook"] == URL
    for bad in ("ftp://x", "http://169.254.169.254/latest", "not a url", 5):
        assert client.patch("/settings/alerts", json={"alertWebhook": bad}, headers=AUTH).status_code == 400
    assert client.patch("/settings/alerts", json={"alertWebhook": ""}, headers=AUTH).json() == {"alertWebhook": None}


@pytest.mark.parametrize("reason, told", [("silence-timed-out", False), ("customer-busy", False),
                                          ("exceeded-max-duration", False), ("vonage-failed-to-connect-call", True)])
def test_only_failures_are_alerts_not_every_unhappy_ending(reason, told):
    store = Store(":memory:")
    store.set_setting(alerts.KEY, URL)
    call(store, "2026-11-17T10:00:00Z", reason)
    assert (run_tell(store)[0] == 1) is told
