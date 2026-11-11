"""QA attacks on campaign retries: restarts, closing windows, old campaigns, edits, several numbers, random outcomes."""

# ruff: noqa: F811  (the imported fixtures are used by name as test arguments)

import random
import time

from fastapi.testclient import TestClient
from test_campaigns import PEOPLE, backdate, end, placed, server, wait  # noqa: F401  (fixtures)

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH

ASKED = {"customer-did-not-answer", "customer-busy"}


def _attempts_by_person(store, campaign_id):
    by_person: dict[str, list[dict]] = {}
    for call in store.list("calls", limit=10_000):
        if call.get("campaignId") == campaign_id:
            by_person.setdefault(call["customer"]["number"], []).append(call)
    return {k: sorted(v, key=lambda c: c["createdAt"]) for k, v in by_person.items()}


def _settings(db):
    return Settings(api_key="test-key", public_url="https://ring.example.com", db_path=db,
                    twilio_account_sid="AC1", twilio_auth_token="tok")


def _number(client):
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH).json()
    return a, n


def test_a_restart_during_the_wait_neither_forgets_nor_repeats_the_retry(placed, tmp_path):
    db = str(tmp_path / "openring.db")
    with TestClient(create_app(_settings(db), Store(db))) as client:
        a, n = _number(client)
        body = {"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE[:1],
                "retryPlan": {"maxAttempts": 2, "delayMinutes": 5}}
        c = client.post("/campaign", json=body, headers=AUTH).json()
        first = wait(client, c["id"], lambda c: len(c["calls"]) == 1)
        end(Store(db), first, 0, "customer-busy")
        wait(client, c["id"], lambda c: c["callsCounterRetrying"] == 1)
    store = Store(db)
    with TestClient(create_app(_settings(db), store)) as client:  # the server restarts while the retry waits
        time.sleep(0.2)
        assert len(placed) == 1  # not called again early, and the campaign is still alive
        assert client.get(f"/campaign/{c['id']}", headers=AUTH).json()["status"] == "in-progress"
        backdate(store, c["id"], 10)
        wait(client, c["id"], lambda c: len(c["calls"]) == 2)
    with TestClient(create_app(_settings(db), store)) as client:  # and again, with the retry already placed
        time.sleep(0.2)
        assert len(placed) == 2  # one retry, not one per restart
        end(store, client.get(f"/campaign/{c['id']}", headers=AUTH).json(), 0, "customer-busy")
        done = wait(client, c["id"], lambda c: c["status"] == "ended")
    assert len(placed) == 2 and done["callsCounterEnded"] == 2


def test_a_window_that_closes_during_the_wait_ends_the_campaign_without_the_retry(server, placed):
    client, store, a, n = server
    body = {"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE[:1], "retryPlan": {"maxAttempts": 3},
            "schedulePlan": {"earliestAt": "2020-01-01T00:00:00Z", "latestAt": "2099-01-01T00:00:00Z"}}
    c = client.post("/campaign", json=body, headers=AUTH).json()
    end(store, wait(client, c["id"], lambda c: len(c["calls"]) == 1), 0, "customer-did-not-answer")
    wait(client, c["id"], lambda c: c["callsCounterRetrying"] == 1)
    store.update("campaigns", c["id"], {"schedulePlan": {"earliestAt": "2020-01-01T00:00:00Z",
                                                          "latestAt": "2020-01-02T00:00:00Z"}})
    backdate(store, c["id"], 120)  # the wait is over too, but the window is not
    done = wait(client, c["id"], lambda c: c["status"] == "ended")
    time.sleep(0.1)
    assert len(placed) == 1 and done["endedReason"] == "campaign.ended.success"


def test_a_campaign_running_since_before_retries_finishes_and_never_retries(placed, tmp_path):
    db = str(tmp_path / "openring.db")
    store = Store(db)
    with TestClient(create_app(_settings(db), store)) as client:
        a, n = _number(client)
    old_call = store.create("calls", {"status": "ended", "endedReason": "customer-did-not-answer",
                                      "customer": {"number": "+5511912340001"}})
    campaign = store.create("campaigns", {
        "name": "old", "phoneNumberId": n["id"], "assistantId": a["id"], "status": "in-progress", "maxConcurrency": 3,
        "customers": [{"number": "+5511912340001"}, {"number": "+5511912340002"}],
        "calls": {old_call["id"]: {"customer": {"number": "+5511912340001"}, "status": "ended",
                                   "endedReason": "customer-did-not-answer"}}})  # no index, no attempt
    with TestClient(create_app(_settings(db), store)) as client:
        started = wait(client, campaign["id"], lambda c: len(c["calls"]) == 2)
        assert placed == ["+5511912340002"]  # the first was already called; nobody is called twice
        end(store, started, 1, "customer-ended-call")
        done = wait(client, campaign["id"], lambda c: c["status"] == "ended")
    assert placed == ["+5511912340002"] and done["callsCounterEnded"] == 2


def test_retry_plan_is_checked_when_a_scheduled_campaign_changes_and_locked_once_it_runs(server, placed):
    client, store, a, n = server
    later = {"schedulePlan": {"earliestAt": "2099-01-01T12:00:00Z"}}
    c = client.post("/campaign", json={"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE, **later},
                    headers=AUTH).json()
    assert "retryPlan" not in c
    ok = client.patch(f"/campaign/{c['id']}", json={"retryPlan": {"maxAttempts": "3", "delayMinutes": 30.9}},
                      headers=AUTH)
    assert ok.status_code == 200 and ok.json()["retryPlan"] == {"maxAttempts": 3, "delayMinutes": 30}
    for bad in ({"maxAttempts": 0}, {"maxAttempts": None}, {"delayMinutes": 4}, {"delayMinutes": 1441},
                {"delayMinutes": "soon"}, {"maxAttempts": 6}, [], "twice", 7):
        r = client.patch(f"/campaign/{c['id']}", json={"retryPlan": bad}, headers=AUTH)
        assert r.status_code == 400, (bad, r.text)
    assert client.patch(f"/campaign/{c['id']}", json={"retryPlan": None}, headers=AUTH).status_code == 200
    assert client.patch(f"/campaign/{c['id']}", json={"retryPlan": {}}, headers=AUTH).json()["retryPlan"] == {
        "maxAttempts": 1, "delayMinutes": 60}  # no retry
    client.patch(f"/campaign/{c['id']}", json={"status": "cancelled"}, headers=AUTH)

    run = client.post("/campaign", json={"name": "y", "phoneNumberId": n["id"], "customers": PEOPLE},
                      headers=AUTH).json()
    wait(client, run["id"], lambda c: c["status"] == "in-progress")
    r = client.patch(f"/campaign/{run['id']}", json={"retryPlan": {"maxAttempts": 5}}, headers=AUTH)
    assert r.status_code == 400  # a campaign that started can only be renamed or cancelled
    client.patch(f"/campaign/{run['id']}", json={"status": "cancelled"}, headers=AUTH)


def test_a_retry_comes_from_the_same_number_and_goes_to_the_back_of_the_line(server, placed):
    client, store, a, n = server
    n2 = client.post("/phone-number", json={"number": "+15557654321", "assistantId": a["id"]}, headers=AUTH).json()
    body = {"name": "x", "assistantId": a["id"], "maxConcurrency": 1, "retryPlan": {"maxAttempts": 2},
            "dialPlan": [{"phoneNumberId": n["id"], "customers": [PEOPLE[0]]},
                         {"phoneNumberId": n2["id"], "customers": [PEOPLE[1], PEOPLE[2]]}]}
    c = client.post("/campaign", json=body, headers=AUTH).json()
    for index in (0, 1, 2):  # one line: each call ends before the next starts
        now = wait(client, c["id"], lambda c, i=index: any(e["index"] == i for e in c["calls"].values()))
        end(store, now, index, "customer-did-not-answer" if index == 1 else "customer-ended-call")
    wait(client, c["id"], lambda c: c["callsCounterRetrying"] == 1 and c["callsCounterEnded"] == 3)
    assert placed == ["+5511912340001", "+5511912340002", "+5511912340003"]  # new people went before any retry
    backdate(store, c["id"], 120)  # past the default wait of an hour
    wait(client, c["id"], lambda c: len(c["calls"]) == 4)
    again = _attempts_by_person(store, c["id"])["+5511912340002"]
    assert len(again) == 2 and {call["phoneNumberId"] for call in again} == {n2["id"]}
    assert again[1]["campaignId"] == c["id"] and again[1]["customer"]["name"] == "João"
    client.patch(f"/campaign/{c['id']}", json={"status": "cancelled"}, headers=AUTH)


def test_whatever_happens_nobody_gets_more_calls_than_the_plan_or_a_retry_they_did_not_earn(server, placed):
    client, store, a, n = server
    rng = random.Random(7)
    reasons = ["customer-did-not-answer", "customer-busy", "voicemail", "customer-ended-call",
               "twilio-failed-to-connect-call", "assistant-ended-call"]
    people = [{"number": f"+551190000{i:04d}"} for i in range(8)]
    c = client.post("/campaign", json={"name": "x", "phoneNumberId": n["id"], "customers": people,
                                       "maxConcurrency": 3, "retryPlan": {"maxAttempts": 3, "delayMinutes": 5}},
                    headers=AUTH).json()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        now = client.get(f"/campaign/{c['id']}", headers=AUTH).json()
        if now["status"] == "ended":
            break
        for call_id, entry in now["calls"].items():
            if entry["status"] != "ended":
                store.claim("calls", call_id, "queued", {"status": "ended", "endedReason": rng.choice(reasons)})
        time.sleep(0.05)
        backdate(store, c["id"], 10)
    else:
        raise AssertionError(f"never ended: {now}")
    by_person = _attempts_by_person(store, c["id"])
    assert len(by_person) == 8
    for number, calls in by_person.items():
        assert 1 <= len(calls) <= 3, (number, [x["endedReason"] for x in calls])
        for before in calls[:-1]:
            assert before["endedReason"] in ASKED, number  # only an unanswered call is ever followed by another
        assert calls[-1]["endedReason"] not in ASKED or len(calls) == 3, number  # and it stops only when it must
    assert len(placed) == sum(len(v) for v in by_person.values())
