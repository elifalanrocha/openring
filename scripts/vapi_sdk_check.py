"""Run Vapi's official Python SDK against an OpenRing server and print what works.

    pip install vapi_server_sdk
    python scripts/vapi_sdk_check.py http://localhost:8000 "$OPENRING_API_KEY"

Every step is one SDK call, and the SDK parses each answer into Vapi's own types, so a step passes only when OpenRing's
JSON is what a Vapi client expects. It creates an assistant, tools, a number (+1 555 123 0001), a squad and a file, and
deletes them again; it rings nobody. Use a server you test on. The result is in docs/api.md ("Checked with Vapi's SDK").
"""
import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from vapi import Vapi

base, key = sys.argv[1], sys.argv[2]
client = Vapi(token=key, base_url=base)
results: list[tuple[str, bool, str]] = []
made: dict[str, str] = {}


def step(name, call):
    try:
        out = call()
        results.append((name, True, ""))
        return out
    except Exception as exc:  # the matrix is the point: keep going
        text = " ".join(str(exc).split())
        results.append((name, False, f"{exc.__class__.__name__}: {text[:260]}"))
        return None


MODEL = {"provider": "groq", "model": "openai/gpt-oss-20b",
         "messages": [{"role": "system", "content": "You are a receptionist."}]}
VOICE = {"provider": "11labs", "voiceId": "21m00Tcm4TlvDq8ikWAM"}
TRANSCRIBER = {"provider": "deepgram", "model": "nova-3", "language": "en"}

# --- assistants ---
a = step("assistants.create", lambda: client.assistants.create(
    name="SDK check", first_message="Hello!", model=MODEL, voice=VOICE, transcriber=TRANSCRIBER,
    end_call_phrases=["goodbye"], max_duration_seconds=300, server={"url": "https://example.com/hook"}))
aid = getattr(a, "id", None)
step("assistants.list", lambda: client.assistants.list())
step("assistants.list(limit)", lambda: client.assistants.list(limit=1))
if aid:
    step("assistants.get", lambda: client.assistants.get(aid))
    step("assistants.update", lambda: client.assistants.update(aid, name="SDK check 2"))

# --- tools ---
t = step("tools.create(function)", lambda: client.tools.create(request={
    "type": "function", "function": {"name": "lookup", "description": "Look an order up",
                                     "parameters": {"type": "object", "properties": {"order": {"type": "string"}}}},
    "server": {"url": "https://example.com/tool"}}))
tid = getattr(t, "id", None)
step("tools.create(endCall)", lambda: client.tools.create(request={"type": "endCall"}))
step("tools.create(transferCall)", lambda: client.tools.create(request={
    "type": "transferCall", "destinations": [{"type": "number", "number": "+15551230009"}]}))
step("tools.list", lambda: client.tools.list())
if tid:
    step("tools.get", lambda: client.tools.get(tid))
    step("tools.update", lambda: client.tools.update(tid, request={
        "type": "function", "function": {"name": "lookup2", "parameters": {"type": "object", "properties": {}}}}))

# --- phone numbers ---
p = step("phone_numbers.create(twilio)", lambda: client.phone_numbers.create(request={
    "provider": "twilio", "number": "+15551230001", "twilioAccountSid": "AC" + "0" * 32, "twilioAuthToken": "x",
    "assistantId": aid}))
pid = getattr(p, "id", None)
step("phone_numbers.list", lambda: client.phone_numbers.list())
if pid:
    step("phone_numbers.get", lambda: client.phone_numbers.get(pid))
    step("phone_numbers.update", lambda: client.phone_numbers.update(pid, request={"name": "Front desk"}))

# --- squads ---
s = step("squads.create", lambda: client.squads.create(name="SDK squad", members=[{"assistantId": aid}]))
sid = getattr(s, "id", None)
step("squads.list", lambda: client.squads.list())
if sid:
    step("squads.get", lambda: client.squads.get(sid))
    step("squads.update", lambda: client.squads.update(sid, name="SDK squad 2", members=[{"assistantId": aid}]))

# --- calls: one made by an incoming call's webhook (no carrier is rung) ---
try:
    form = urllib.parse.urlencode({"To": "+15551230001", "From": "+15551230002", "CallSid": "CAsdkcheck"}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{base}/twilio/voice", data=form), timeout=10).read()
except Exception as exc:
    print("could not make a call record:", exc)
calls = step("calls.list", lambda: client.calls.list())
step("calls.list(assistant_id, limit)", lambda: client.calls.list(assistant_id=aid, limit=5))
cid = calls[0].id if calls else None
if cid:
    step("calls.get", lambda: client.calls.get(cid))
    step("calls.update", lambda: client.calls.update(cid, name="Checked"))
    step("calls.delete", lambda: client.calls.delete(cid))
else:
    results.append(("calls.get/update/delete", False, "no call record to try"))

# --- files ---
with tempfile.TemporaryDirectory() as folder:
    doc = Path(folder) / "hours.txt"
    doc.write_text("We open at nine and close at six.", encoding="utf-8")
    with doc.open("rb") as handle:
        f = step("files.create", lambda: client.files.create(file=handle))
fid = getattr(f, "id", None)
step("files.list", lambda: client.files.list())
if fid:
    step("files.get", lambda: client.files.get(fid))
    step("files.update", lambda: client.files.update(fid, name="hours"))
    step("files.delete", lambda: client.files.delete(fid))

# --- campaigns (listing only: creating one would start dialing) ---
step("campaigns.find_all", lambda: client.campaigns.campaign_controller_find_all())

# --- what OpenRing doesn't have, to say so with a number ---
step("knowledge_bases_v_2.find_all", lambda: client.knowledge_bases_v_2.knowledge_base_v_2_controller_find_all())
step("analytics.get", lambda: client.analytics.get(queries=[{
    "table": "call", "name": "calls", "operations": [{"operation": "count", "column": "id"}]}]))
step("chats.list", lambda: client.chats.list())
step("sessions.list", lambda: client.sessions.list())

# --- clean up, which is also the delete calls ---
if sid:
    step("squads.delete", lambda: client.squads.delete(sid))
if pid:
    step("phone_numbers.delete", lambda: client.phone_numbers.delete(pid))
if tid:
    step("tools.delete", lambda: client.tools.delete(tid))
if aid:
    step("assistants.delete", lambda: client.assistants.delete(aid))

passed = sum(ok for _, ok, _ in results)
for name, ok, why in results:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  <- {why}" if why else ""))
print(json.dumps({"passed": passed, "total": len(results)}))
