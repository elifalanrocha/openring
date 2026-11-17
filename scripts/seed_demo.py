"""Fill a fresh OpenRing database with realistic example data, to see every dashboard page populated.

    python scripts/seed_demo.py path/to/demo.db

Everything is made up: phone numbers are in the +1 555 01xx fiction range, no call ever happened, no provider is
contacted. Run a server on that database (OPENRING_DB_PATH=path/to/demo.db openring --port 8001) to look at it; never
point it at your own database.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openring.store import Store  # noqa: E402

rng = random.Random(7)
NOW = datetime.now(UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def made(store: Store, collection: str, doc: dict, when: datetime) -> dict:
    """Create a document as if it had been made at `when`."""
    doc = store.create(collection, doc)
    stamp = iso(when)
    doc = {**doc, "createdAt": stamp, "updatedAt": stamp}
    db = sqlite3.connect(store_path)
    db.execute(f"UPDATE {collection} SET created_at = ?, doc = ? WHERE id = ?", (stamp, json.dumps(doc), doc["id"]))
    db.commit()
    db.close()
    return doc


def number() -> str:
    return f"+1555010{rng.randint(0, 99):02d}"


ASSISTANTS = [
    ("Front desk: Bright Smile Dental", "Thank you for calling Bright Smile Dental, this is Ava. How can I help you today?",
     {"provider": "deepgram", "model": "nova-3", "language": "en"}, {"provider": "groq", "model": "openai/gpt-oss-120b"},
     {"provider": "kokoro", "voiceId": "af_heart"}),
    ("Sales: Northwind Solar", "Hi, this is Leo from Northwind Solar. Is now a good time for a quick chat about your energy bill?",
     {"provider": "deepgram", "model": "nova-3", "language": "en"}, {"provider": "openai", "model": "gpt-4.1-mini"},
     {"provider": "11labs", "voiceId": "hpp4J3VqNfWAUOO0d1Us", "model": "eleven_flash_v2_5"}),
    ("Support: Parcel Express", "Parcel Express support, I'm Mia. Do you have your tracking number handy?",
     {"provider": "assembly-ai", "language": "en"}, {"provider": "anthropic", "model": "claude-haiku-5-5"},
     {"provider": "cartesia", "voiceId": "a0e99841-438c-4a64-b679-ae501e7d6091"}),
    ("Recepção: Clínica Vida (pt-BR)", "Olá, Clínica Vida, aqui é a Bia. Em que posso ajudar?",
     {"provider": "deepgram", "model": "nova-3", "language": "pt-BR"}, {"provider": "groq", "model": "openai/gpt-oss-20b"},
     {"provider": "kokoro", "voiceId": "pf_dora"}),
    ("Private and free: local stack", "Hello! Everything on this call runs on this computer. What can I do for you?",
     {"provider": "whisper", "model": "base"}, {"provider": "ollama", "model": "llama3.2"},
     {"provider": "kokoro", "voiceId": "af_heart"}),
]
SCRIPTS = [
    [("user", "Hi, I'd like to book a cleaning for next week."), ("assistant", "Of course. Do mornings or afternoons work better?"),
     ("user", "Tuesday morning, if you have it."), ("assistant", "Tuesday at 9:30 is open. Shall I book it under your name?"),
     ("user", "Yes please, it's Daniel Reyes."), ("assistant", "Done, Daniel. You'll get a text reminder the day before.")],
    [("assistant", "Hi, this is Leo from Northwind Solar. Is now a good time?"), ("user", "Sure, what's this about?"),
     ("assistant", "We can cut most homes' bills by a third. What do you pay a month, roughly?"),
     ("user", "About two hundred dollars."), ("assistant", "Then a free roof check could save you a lot. Can I book one?"),
     ("user", "Thursday afternoon works."), ("assistant", "Booked for Thursday at 3. Thanks!")],
    [("user", "My package says delivered but it isn't here."), ("assistant", "I'm sorry. What's the tracking number?"),
     ("user", "PX 4 4 1 9 0 2."), ("assistant", "It was left at the side door at 2:14 pm. Could you check there?"),
     ("user", "Found it, thanks!"), ("assistant", "Glad it turned up. Anything else?")],
    [("user", "Oi, queria remarcar minha consulta de sexta."), ("assistant", "Claro! Para qual dia fica melhor?"),
     ("user", "Segunda de manhã."), ("assistant", "Segunda às 10h está livre. Posso confirmar?"), ("user", "Pode sim."),
     ("assistant", "Confirmado. Até segunda!")],
    [("user", "Can you summarize what you can do?"), ("assistant", "I answer questions and take messages, all offline."),
     ("user", "Nice. Take a message for Sam: call me back."), ("assistant", "Saved for Sam. Anything else?")],
]
ENDINGS = [("customer-ended-call", 58), ("assistant-ended-call", 16), ("assistant-forwarded-call", 8),
           ("customer-did-not-answer", 6), ("silence-timed-out", 5), ("customer-busy", 3),
           ("pipeline-error: the model (openai gpt-4.1-mini) stopped working: HTTP 429: rate limit", 4)]


def seed(store: Store) -> None:
    start = NOW - timedelta(days=21)
    tools = [made(store, "tools", {"type": "function", "function": {"name": "book_appointment",
             "description": "Book a visit", "parameters": {"type": "object", "properties": {"day": {"type": "string"},
             "time": {"type": "string"}}, "required": ["day"]}}, "server": {"url": "https://example.com/tools"}}, start),
             made(store, "tools", {"type": "transferCall", "destinations": [{"type": "number", "number": "+15550100",
                  "message": "Connecting you to a person now."}]}, start),
             made(store, "tools", {"type": "endCall"}, start)]
    file = made(store, "files", {"name": "dental-faq.md", "originalName": "dental-faq.md", "bytes": 4210,
                "status": "done", "mimetype": "text/markdown", "parsedTextBytes": 4100, "passages": 3}, start)
    store.index_file(file["id"], ["We are open Monday to Friday, 8 am to 6 pm, and Saturday mornings.",
                                  "A cleaning takes about 45 minutes and costs $90 without insurance.",
                                  "We accept Delta Dental, Cigna and Aetna plans."])
    made(store, "knowledge_bases", {"name": "Clinic FAQ", "provider": "openring", "fileIds": [file["id"]]}, start)
    assistants = []
    for i, (name, first, stt, llm, tts) in enumerate(ASSISTANTS):
        doc = {"name": name, "firstMessage": first, "transcriber": stt, "voice": tts,
               "model": {**llm, "messages": [{"role": "system", "content": f"You are the voice assistant of {name}."}],
                         "toolIds": [t["id"] for t in tools[: 1 + i % 3]]},
               "analysisPlan": {"summaryPlan": {"enabled": True}, "successEvaluationPlan": {"enabled": True}},
               "endCallPhrases": ["goodbye"], "maxDurationSeconds": 600}
        assistants.append(made(store, "assistants", doc, start + timedelta(hours=i)))
    made(store, "squads", {"name": "Dental clinic team", "members": [
        {"assistantId": assistants[0]["id"], "assistantDestinations": [{"type": "assistant", "assistantName": assistants[2]["name"],
         "message": "Let me pass you to billing.", "description": "Questions about bills"}]},
        {"assistantId": assistants[2]["id"]}]}, start + timedelta(days=1))
    numbers = [made(store, "phone_numbers", {"number": "+15550101", "provider": "twilio", "name": "Main line",
                    "assistantId": assistants[0]["id"]}, start),
               made(store, "phone_numbers", {"number": "+15550102", "provider": "telnyx", "name": "Sales line",
                    "assistantId": assistants[1]["id"]}, start),
               made(store, "phone_numbers", {"number": "+15550103", "provider": "vonage", "name": "Support",
                    "assistantId": assistants[2]["id"]}, start)]
    made(store, "routes", {"name": "Fast answers", "kind": "model", "preset": "fastest", "models": [
        {"provider": "groq", "model": "openai/gpt-oss-20b"}, {"provider": "cerebras", "model": "gpt-oss-120b"},
        {"provider": "openai", "model": "gpt-4.1-mini"}]}, start)
    made(store, "routes", {"name": "Cheapest first", "kind": "model", "preset": "cheapest", "models": [
        {"provider": "groq", "model": "openai/gpt-oss-20b"}, {"provider": "deepseek", "model": "deepseek-chat"}]}, start)
    outputs = [made(store, "structured_outputs", {"name": "Appointment booked", "type": "ai", "schema": {"type": "boolean"},
                    "assistantIds": [assistants[0]["id"]]}, start),
               made(store, "structured_outputs", {"name": "Tracking number", "type": "regex", "regex": "PX ?[0-9 ]{6,}",
                    "assistantIds": [assistants[2]["id"]]}, start)]
    evals = [made(store, "evals", {"name": "Books a cleaning", "type": "chat.mockConversation", "messages": [
        {"role": "user", "content": "Can I book a cleaning on Tuesday?"},
        {"role": "assistant", "judgePlan": {"type": "ai", "messages": [{"role": "system",
         "content": "Pass if the assistant offers a time on Tuesday."}]}}]}, start),
             made(store, "evals", {"name": "Says the opening hours", "type": "chat.mockConversation", "messages": [
        {"role": "user", "content": "When are you open?"},
        {"role": "assistant", "judgePlan": {"type": "regex", "content": "8 ?am"}}]}, start)]
    for i, e in enumerate(evals):
        for k in range(3):
            passed = not (i == 1 and k == 0)
            made(store, "eval_runs", {"evalId": e["id"], "target": {"type": "assistant", "assistantId": assistants[0]["id"]},
                 "status": "ended", "endedReason": "mockConversation.done",
                 "results": [{"status": "pass" if passed else "fail", "messages": []}]}, NOW - timedelta(days=6 - k * 2, hours=i))

    calls = []
    for day in range(20, -1, -1):
        for _ in range(rng.choice([0, 1, 2, 2, 3, 4, 5, 6])):
            a = rng.randrange(len(assistants))
            kind = rng.choices(["inboundPhoneCall", "outboundPhoneCall", "webCall"], [5, 3, 2])[0]
            when = NOW - timedelta(days=day, hours=rng.randint(0, 9), minutes=rng.randint(0, 59))
            reason = rng.choices([r for r, _ in ENDINGS], [w for _, w in ENDINGS])[0]
            reached = reason not in ("customer-did-not-answer", "customer-busy")
            seconds = rng.randint(35, 380) if reached else 0
            script = SCRIPTS[a][: rng.randint(3, len(SCRIPTS[a]))] if reached else []
            turns = [{"endpointingLatency": rng.randint(170, 420), "transcriberLatency": rng.randint(60, 260),
                      "modelLatency": rng.randint(240, 760), "voiceLatency": rng.randint(180, 900)} for _ in script[1::2]]
            for t in turns:
                t["turnLatency"] = sum(t.values()) + rng.randint(20, 80)
            minutes = seconds / 60
            stt, llm, tts = round(minutes * 0.0077, 5), round(minutes * rng.uniform(0.0008, 0.012), 5), round(minutes * (0 if a in (0, 3, 4) else 0.03), 5)
            line = round(minutes * 0.0129, 5) if kind != "webCall" else 0
            success = "true" if reason in ("customer-ended-call", "assistant-ended-call", "assistant-forwarded-call") and rng.random() > .12 else "false"
            call = {"type": kind, "status": "ended", "assistantId": assistants[a]["id"],
                    "customer": {"number": number()} if kind != "webCall" else {},
                    **({"phoneNumberId": numbers[a % 3]["id"], "phoneCallProvider": numbers[a % 3]["provider"]} if kind != "webCall" else {}),
                    "startedAt": iso(when), "endedAt": iso(when + timedelta(seconds=seconds)), "durationSeconds": seconds,
                    "endedReason": reason, "cost": round(stt + llm + tts + line, 5),
                    "costBreakdown": {"stt": stt, "llm": llm, "tts": tts, **({"line": line} if line else {}),
                                      "total": round(stt + llm + tts + line, 5), "llmPromptTokens": 1100 * len(turns),
                                      "llmCompletionTokens": 48 * len(turns), "ttsCharacters": 90 * len(turns),
                                      "transcriberAudioSeconds": seconds},
                    "artifact": {"messages": [{"role": r, "message": m} for r, m in script],
                                 "performanceMetrics": {"turnLatencies": turns, **({"firstWordLatency": rng.randint(800, 2100)} if reached else {})}},
                    **({"analysis": {"summary": f"The caller {('booked a visit' if a == 0 else 'got what they needed')}.",
                                     "successEvaluation": success}} if reached else {})}
            calls.append(made(store, "calls", call, when))
    live = made(store, "calls", {"type": "inboundPhoneCall", "status": "in-progress", "assistantId": assistants[0]["id"],
                "customer": {"number": number()}, "phoneNumberId": numbers[0]["id"], "startedAt": iso(NOW)}, NOW)

    outbound = [c for c in calls if c["type"] == "outboundPhoneCall"]
    for name, chosen, status in (("Spring roof check-ups", outbound[:9], "ended"), ("Reminder: overdue cleanings", outbound[9:15], "ended")):
        entries = {c["id"]: {"status": "ended", "endedReason": c["endedReason"], "index": i, "attempt": 1,
                             "customer": {"number": c["customer"]["number"]}} for i, c in enumerate(chosen)}
        made(store, "campaigns", {"name": name, "assistantId": assistants[1]["id"], "phoneNumberId": numbers[1]["id"],
             "customers": [e["customer"] for e in entries.values()], "status": status, "endedReason": "campaign.ended.success",
             "calls": entries, "callsCounterScheduled": 0, "callsCounterQueued": 0, "callsCounterInProgress": 0,
             "callsCounterRetrying": 0, "callsCounterEndedVoicemail": 0, "callsCounterEnded": len(entries),
             "retryPlan": {"maxAttempts": 2, "delayMinutes": 60}}, NOW - timedelta(days=rng.randint(2, 9)))
    made(store, "escalations", {"name": "Server room temperature alert", "phoneNumberId": numbers[0]["id"],
         "assistantId": assistants[4]["id"], "customers": [{"number": "+15550170", "name": "On-call: Priya"},
         {"number": "+15550171", "name": "Backup: Tom"}], "status": "ended", "endedReason": "acknowledged",
         "attempts": [{"round": 1, "number": "+15550170", "callId": outbound[-1]["id"]},
                      {"round": 1, "number": "+15550171", "callId": outbound[-2]["id"]}]}, NOW - timedelta(days=3))
    print(f"seeded: {len(assistants)} assistants, {len(calls) + 1} calls (one live: {live['id'][:8]}), "
          f"{len(outputs)} outputs, {len(evals)} evals, 2 campaigns, 1 escalation")


if __name__ == "__main__":
    store_path = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
    if Path(store_path).exists():
        sys.exit(f"{store_path} exists; give a new path so nothing real is overwritten")
    seed(Store(store_path))
