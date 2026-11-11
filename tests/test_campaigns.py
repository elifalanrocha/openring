"""Campaigns (Vapi's /campaign): a list of customers called a few at a time. Twilio is a stub, and the dialer
runs fast so a whole campaign fits in a test."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from openring import campaigns, overrides, twilio
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH

PEOPLE = [{"number": "+55 11 91234-0001", "name": "Maria"}, {"number": "+55 11 91234-0002", "name": "João"},
          {"number": "+55 11 91234-0003"}]


@pytest.fixture
def placed(monkeypatch):
    calls = []

    async def create_call(account_sid, auth_token, *, to, from_, url, status_callback, machine_detection=None):
        calls.append(to)
        if to.endswith("9999"):
            raise twilio.TwilioError("Trial accounts may only make calls to verified numbers. (Twilio error 21219)")
        return f"CA-{len(calls)}"

    monkeypatch.setattr(twilio, "create_call", create_call)
    monkeypatch.setattr(campaigns, "TICK", 0.01)
    monkeypatch.setattr(campaigns, "DIAL_GAP", 0)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    return calls


@pytest.fixture
def server(placed):
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://ring.example.com", db_path=":memory:",
                        twilio_account_sid="AC1", twilio_auth_token="tok")
    with TestClient(create_app(settings, store)) as client:  # `with` keeps the dialer's event loop running
        a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
        n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH).json()
        yield client, store, a, n


def wait(client, campaign_id, until, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        c = client.get(f"/campaign/{campaign_id}", headers=AUTH).json()
        if until(c):
            return c
        time.sleep(0.02)
    raise AssertionError(f"campaign never got there: {c}")


def hang_up(store, campaign):
    """The calls end, as the bot or Twilio's status callback would end them."""
    for call_id in campaign["calls"]:
        store.claim("calls", call_id, "queued", {"status": "ended", "endedReason": "customer-ended-call"})


def test_a_campaign_calls_everyone_a_few_at_a_time_then_ends(server, placed):
    client, store, a, n = server
    r = client.post("/campaign", json={"name": "Lembrete", "phoneNumberId": n["id"], "assistantId": a["id"],
                                       "customers": PEOPLE, "maxConcurrency": 2}, headers=AUTH)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] in campaigns.LIVE and created["callsCounterScheduled"] == 3
    assert created["customers"][0]["number"] == "+5511912340001"  # stored in E.164

    first = wait(client, created["id"], lambda c: c["callsCounterQueued"] == 2)
    time.sleep(0.1)
    assert len(placed) == 2  # two lines busy: the third waits
    hang_up(store, first)
    second = wait(client, created["id"], lambda c: len(c["calls"]) == 3)
    hang_up(store, second)
    done = wait(client, created["id"], lambda c: c["status"] == "ended")
    assert done["endedReason"] == "campaign.ended.success" and done["callsCounterEnded"] == 3
    assert placed == ["+5511912340001", "+5511912340002", "+5511912340003"]

    call = store.get("calls", next(iter(done["calls"])))
    assert call["campaignId"] == created["id"] and call["customer"] == {"number": "+5511912340001", "name": "Maria"}
    listed = client.get("/campaign", headers=AUTH).json()
    assert listed["metadata"]["totalItems"] == 1 and listed["results"][0]["id"] == created["id"]


def test_a_refused_call_is_counted_and_the_campaign_goes_on(server, placed):
    client, store, a, n = server
    people = [{"number": "+5511900009999"}, {"number": "+5511900000002"}]
    c = client.post("/campaign", json={"name": "x", "phoneNumberId": n["id"], "customers": people}, headers=AUTH).json()
    running = wait(client, c["id"], lambda c: len(c["calls"]) == 2)
    hang_up(store, running)
    done = wait(client, c["id"], lambda c: c["status"] == "ended")
    reasons = sorted(e.get("endedReason") for e in done["calls"].values())
    assert reasons == ["customer-ended-call", "twilio-failed-to-connect-call"]


def test_a_scheduled_campaign_waits_and_can_be_cancelled(server, placed):
    client, _, a, n = server
    body = {"name": "Amanhã", "phoneNumberId": n["id"], "customers": PEOPLE,
            "schedulePlan": {"earliestAt": "2099-01-01T12:00:00Z"}}
    c = client.post("/campaign", json=body, headers=AUTH).json()
    time.sleep(0.1)
    assert placed == [] and client.get(f"/campaign/{c['id']}", headers=AUTH).json()["status"] == "scheduled"
    assert client.patch(f"/campaign/{c['id']}", json={"maxConcurrency": 3}, headers=AUTH).json()["maxConcurrency"] == 3
    stopped = client.patch(f"/campaign/{c['id']}", json={"status": "cancelled"}, headers=AUTH).json()
    assert (stopped["status"], stopped["endedReason"]) == ("cancelled", "campaign.scheduled.ended-by-user")
    assert client.patch(f"/campaign/{c['id']}", json={"status": "cancelled"}, headers=AUTH).status_code == 400
    assert client.patch(f"/campaign/{c['id']}", json={"customers": PEOPLE}, headers=AUTH).status_code == 400
    assert client.patch(f"/campaign/{c['id']}", json={"name": "Renamed"}, headers=AUTH).json()["name"] == "Renamed"


def test_cancelling_a_running_campaign_stops_new_calls(server, placed):
    client, store, a, n = server
    c = client.post("/campaign", json={"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE,
                                       "maxConcurrency": 1}, headers=AUTH).json()
    wait(client, c["id"], lambda c: c["callsCounterQueued"] == 1)
    stopped = client.patch(f"/campaign/{c['id']}", json={"status": "ended"}, headers=AUTH).json()  # Vapi's older word
    assert (stopped["status"], stopped["endedReason"]) == ("cancelled", "campaign.in-progress.ended-by-user")
    hang_up(store, stopped)
    time.sleep(0.15)
    assert len(placed) == 1


def test_the_window_closes_on_latest_at(server, placed):
    client, store, a, n = server
    body = {"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE,
            "schedulePlan": {"earliestAt": "2020-01-01T00:00:00Z", "latestAt": "2020-01-02T00:00:00Z"}}
    c = client.post("/campaign", json=body, headers=AUTH).json()
    done = wait(client, c["id"], lambda c: c["status"] == "ended")
    assert placed == [] and done["callsCounterScheduled"] == 3  # nobody called after the window


def test_campaign_bodies_are_checked_like_a_run_would(server):
    client, _, a, n = server
    ok = {"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE}
    for change, words in (
        ({"name": ""}, "name"),
        ({"customers": []}, "customers"),
        ({"customers": [{"name": "no number"}]}, "customers[0].number"),
        ({"phoneNumberId": None}, "phoneNumberId"),
        ({"maxConcurrency": 501}, "maxConcurrency"),
        ({"schedulePlan": {"latestAt": "2026-11-03T13:00:00Z"}}, "earliestAt"),
        ({"schedulePlan": {"earliestAt": "tomorrow"}}, "ISO 8601"),
        ({"schedulePlan": {"earliestAt": "2026-11-03T13:00:00Z", "latestAt": "2026-11-03T12:00:00Z"}}, "latestAt"),
        ({"squadId": "s1"}, "squadId"),
        ({"retryPlan": {"maxAttempts": 6}}, "maxAttempts"),
        ({"retryPlan": {"delayMinutes": 1}}, "delayMinutes"),
        ({"retryPlan": "twice"}, "retryPlan"),
        ({"dialPlan": [{"phoneNumberId": n["id"], "customers": PEOPLE}]}, "not both"),
        ({"customers": [{"number": "+5511912340001", "assistantOverrides": {"voice": {"provider": "playht"}}}]},
         "playht"),
    ):
        r = client.post("/campaign", json={**ok, **change}, headers=AUTH)
        assert r.status_code == 400 and words in r.json()["detail"], (change, r.text)
    assert client.post("/campaign", json={**ok, "phoneNumberId": "nope"}, headers=AUTH).status_code == 404
    plan = {"name": "x", "dialPlan": [{"phoneNumberId": n["id"], "customers": PEOPLE}]}
    assert client.post("/campaign", json=plan, headers=AUTH).status_code == 201


def test_campaigns_need_twilio_and_a_public_url(placed):
    for missing, word in (({"twilio_auth_token": ""}, "TWILIO_AUTH_TOKEN"), ({"public_url": ""}, "PUBLIC_URL")):
        settings = Settings(**{"api_key": "test-key", "public_url": "https://ring.example.com", "db_path": ":memory:",
                               "twilio_account_sid": "AC1", "twilio_auth_token": "tok", **missing})
        client = TestClient(create_app(settings, Store(":memory:")))
        a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
        n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH).json()
        r = client.post("/campaign", json={"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE}, headers=AUTH)
        assert r.status_code == 400 and word in r.json()["detail"]


def test_a_restarted_server_resumes_without_calling_anyone_twice(placed, tmp_path):
    db = str(tmp_path / "openring.db")
    settings = Settings(api_key="test-key", public_url="https://ring.example.com", db_path=db,
                        twilio_account_sid="AC1", twilio_auth_token="tok")
    with TestClient(create_app(settings, Store(db))) as client:
        a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
        n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH).json()
        c = client.post("/campaign", json={"name": "x", "phoneNumberId": n["id"], "customers": PEOPLE,
                                           "maxConcurrency": 1}, headers=AUTH).json()
        first = wait(client, c["id"], lambda c: c["callsCounterQueued"] == 1)
    store = Store(db)
    live = next(iter(first["calls"]))
    store.update("calls", live, {"status": "in-progress", "startedAt": "2020-01-01T00:00:00Z"})  # cut off mid-call
    with TestClient(create_app(settings, store)) as client:
        resumed = wait(client, c["id"], lambda c: len(c["calls"]) == 2)
        assert store.get("calls", live)["endedReason"] == "worker-shutdown"
        hang_up(store, resumed)
        hang_up(store, wait(client, c["id"], lambda c: len(c["calls"]) == 3))
        wait(client, c["id"], lambda c: c["status"] == "ended")
    assert placed == ["+5511912340001", "+5511912340002", "+5511912340003"]


def test_variables_fill_the_greeting_and_prompt():
    assistant = {"firstMessage": "Oi {{ customer.name }}, aqui é da {{empresa}}.",
                 "model": {"provider": "groq",
                           "messages": [{"role": "system", "content": "Fale com {{customer.name}}."}]}}
    out = overrides.apply(assistant, {"variableValues": {"empresa": "Clínica Sol"}}, {"name": "Maria"})
    assert out["firstMessage"] == "Oi Maria, aqui é da Clínica Sol."
    assert out["model"]["messages"][0]["content"] == "Fale com Maria."
    assert overrides.apply(assistant)["firstMessage"] == "Oi , aqui é da ."  # unknown variables say nothing
    idle = {"messagePlan": {"idleMessages": ["{{customer.name}}, ainda aí?"], "idleTimeoutSeconds": 8}}
    assert overrides.apply(idle, None, {"name": "Maria"})["messagePlan"] == {  # never read aloud with the braces
        "idleMessages": ["Maria, ainda aí?"], "idleTimeoutSeconds": 8}
    assert assistant["firstMessage"].startswith("Oi {{")  # the saved assistant is untouched
    merged = overrides.apply(assistant, {"model": {"temperature": 0.2}})
    assert merged["model"]["temperature"] == 0.2 and merged["model"]["provider"] == "groq"
    assert overrides.combine({"variableValues": {"a": 1}}, {"variableValues": {"b": 2}}) == {
        "variableValues": {"a": 1, "b": 2}}


def test_nobody_is_called_twice_in_one_campaign(server, placed):
    client, store, a, n = server
    # The same person written two ways, and again in another dialPlan entry.
    people = [{"number": "+55 11 91234-0001"}, {"number": "+5511912340001"}, {"number": "+5511912340002"}]
    r = client.post("/campaign", json={"name": "Twice", "phoneNumberId": n["id"], "assistantId": a["id"],
                                       "customers": people}, headers=AUTH)
    assert r.status_code == 201, r.text
    assert [c["number"] for c in r.json()["customers"]] == ["+5511912340001", "+5511912340002"]
    plan = {"name": "Plan", "assistantId": a["id"], "dialPlan": [
        {"phoneNumberId": n["id"], "customers": [{"number": "+5511912340003"}]},
        {"phoneNumberId": n["id"], "customers": [{"number": "+55 11 91234-0003"}, {"number": "+5511912340004"}]}]}
    r = client.post("/campaign", json=plan, headers=AUTH)
    assert r.status_code == 201, r.text
    assert [len(e["customers"]) for e in r.json()["dialPlan"]] == [1, 1]
    for campaign in client.get("/campaign", headers=AUTH).json()["results"]:
        client.patch(f"/campaign/{campaign['id']}", json={"status": "cancelled"}, headers=AUTH)


def test_customer_numbers_must_be_phone_numbers(server):
    client, _, a, n = server
    for bad in ("+1", "call me", "+12345678901234567"):
        r = client.post("/campaign", json={"name": "Bad", "phoneNumberId": n["id"], "assistantId": a["id"],
                                           "customers": [{"number": bad}]}, headers=AUTH)
        assert r.status_code == 400 and "country code" in r.json()["detail"], bad
        r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": bad}}, headers=AUTH)
        assert r.status_code == 400 and "country code" in r.json()["detail"], bad


def end(store, campaign, number_index, reason):
    """One call ends the way Twilio's status callback would end it."""
    call_id = [i for i, e in campaign["calls"].items() if e["index"] == number_index and e["status"] != "ended"][0]
    store.claim("calls", call_id, "queued", {"status": "ended", "endedReason": reason})


def backdate(store, campaign_id, minutes):
    """The ended calls ended `minutes` ago, so a retry's wait is over."""
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    calls = store.get("campaigns", campaign_id)["calls"]
    store.update("campaigns", campaign_id, {"calls": {k: {**v, "endedAt": old} if v["status"] == "ended" else v
                                                      for k, v in calls.items()}})


def test_someone_who_did_not_answer_is_called_again_after_the_wait(server, placed):
    client, store, a, n = server
    body = {"name": "Retry", "phoneNumberId": n["id"], "customers": PEOPLE[:2], "maxConcurrency": 5,
            "retryPlan": {"maxAttempts": 2, "delayMinutes": 5}}
    c = client.post("/campaign", json=body, headers=AUTH).json()
    assert c["retryPlan"] == {"maxAttempts": 2, "delayMinutes": 5}
    first = wait(client, c["id"], lambda c: len(c["calls"]) == 2)
    end(store, first, 0, "customer-did-not-answer")
    end(store, first, 1, "customer-ended-call")
    waiting = wait(client, c["id"], lambda c: c["callsCounterRetrying"] == 1)
    time.sleep(0.1)
    assert len(placed) == 2 and waiting["status"] == "in-progress"  # the wait is not over: no call, campaign open

    backdate(store, c["id"], 10)
    again = wait(client, c["id"], lambda c: len(c["calls"]) == 3)
    assert placed[2] == "+5511912340001"
    assert sorted((e["index"], e["attempt"]) for e in again["calls"].values()) == [(0, 1), (0, 2), (1, 1)]
    end(store, again, 0, "customer-did-not-answer")  # the last attempt: nobody is called a third time
    done = wait(client, c["id"], lambda c: c["status"] == "ended")
    assert done["endedReason"] == "campaign.ended.success" and len(placed) == 3 and done["callsCounterRetrying"] == 0


def test_only_busy_and_unanswered_calls_are_retried(server, placed):
    client, store, a, n = server
    people = [{"number": f"+551190000{i:04d}"} for i in range(4)]
    body = {"name": "Which", "phoneNumberId": n["id"], "customers": people, "maxConcurrency": 5,
            "retryPlan": {"maxAttempts": 3}}
    c = client.post("/campaign", json=body, headers=AUTH).json()
    assert c["retryPlan"]["delayMinutes"] == 60
    started = wait(client, c["id"], lambda c: len(c["calls"]) == 4)
    for i, reason in enumerate(("customer-busy", "voicemail", "twilio-failed-to-connect-call", "customer-ended-call")):
        end(store, started, i, reason)
    waiting = wait(client, c["id"], lambda c: c["callsCounterEnded"] == 4)
    assert waiting["callsCounterRetrying"] == 1 and waiting["callsCounterScheduled"] == 0
    client.patch(f"/campaign/{c['id']}", json={"status": "cancelled"}, headers=AUTH)


def test_calls_saved_before_retries_existed_still_count():
    campaign = {"customers": PEOPLE, "calls": {"a": {"status": "ended"}, "b": {"status": "queued"}}}
    counted = campaigns.counters(campaign)
    assert counted["callsCounterScheduled"] == 1 and counted["callsCounterRetrying"] == 0
