# OpenRing API guide

Everything here is in Vapi's JSON shape. The [README](../README.md) has the quick start, the comparison and the provider list.

Every request takes `Authorization: Bearer $OPENRING_API_KEY`.

### Assistants

Same shape as Vapi:

```bash
curl -X POST http://localhost:8000/assistant \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Front desk",
    "firstMessage": "Hi! How can I help you today?",
    "model": {"provider": "openai", "model": "gpt-4.1-mini",
              "messages": [{"role": "system", "content": "You are a friendly receptionist."}]},
    "voice": {"provider": "11labs", "voiceId": "YOUR_VOICE_ID"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"}
  }'
```

A call ends by itself after `maxDurationSeconds` (10 minutes by default), or after `silenceTimeoutSeconds` without
activity (30 seconds by default; the clock waits while someone talks, the model thinks or a tool runs), with Vapi's
`endedReason` (`exceeded-max-duration`, `silence-timed-out`); `endCallMessage` is said first. When the caller goes
quiet after the assistant spoke,
`messagePlan.idleMessages` are said (one at random, after `idleTimeoutSeconds`, up to
`idleMessageMaxSpokenCount` times), as on Vapi:

```json
{"maxDurationSeconds": 900, "silenceTimeoutSeconds": 20, "endCallMessage": "Thanks for calling, goodbye!",
 "messagePlan": {"idleMessages": ["Are you still there?"], "idleTimeoutSeconds": 8}}
```

### Phone calls for AI agents (MCP)

`openring mcp` is an [MCP](https://modelcontextprotocol.io) server over stdio: an AI agent such as Claude or Cursor can
place a call through your OpenRing and read the transcript. It calls the running server at `OPENRING_URL`
(`http://localhost:8000` by default) with `OPENRING_API_KEY` (read from `~/.openring/.env` when not set). For Claude Code:

```bash
claude mcp add openring -- npx openring mcp
```

Tools: `list_assistants`, `list_phone_numbers`, `place_call` (`phoneNumberId`, `number`, and optionally `assistantId`, the
person's `name` and a `context` the assistant gets as `{{context}}`), `get_call` (status, ended reason, duration, cost and
transcript) and `list_calls`. `place_call` rings a real person and costs money; its description tells the agent to
confirm with you first.

### Callers that are AI agents

More and more callers are assistants calling for a person ("Hi, I'm an AI assistant calling on behalf of Maria"). OpenRing
notices it from what they say, in English, Portuguese or Spanish, and marks the call (`callerIsAI`, and `callerIsAISaid`
with the words that showed it; the dashboard writes "AI agent" before the caller). The model is then told to answer
briefly and exactly, without small talk, never to share other customers' details and never to take payment details from
it. OpenRing's own field, on by default: `"aiCallerPlan": {"enabled": false}` keeps the model's usual manner (the call is
still marked). Beta: noticed by phrases, not yet tried with a real agent calling.

### Writing an assistant from a description

OpenRing's own. `POST /assistant/draft` with `{"description": "..."}` asks a model you connected (the first of Groq, OpenAI,
OpenRouter, Anthropic, Gemini and others with a key, or `model: {provider, model}` to choose) for a name, a greeting and a
system prompt, and returns them in the assistant's shape. Nothing is saved. In the dashboard, a new assistant has a
**Describe it** box that fills the form. The model is told never to invent prices, hours or policies the description
doesn't give.

### When the assistant talks, and keys the caller presses

Vapi's fields, beta, checked on simulated calls only:

- `startSpeakingPlan.waitSeconds` (0 to 5): the silence after the caller stops before the assistant answers. It sets the
  voice activity detector's stop time, an approximation of Vapi's setting.
- `stopSpeakingPlan.voiceSeconds` (0 to 0.5): how long the caller must speak to count as speaking (0.2 by default on both).
- `stopSpeakingPlan.numWords` (0 to 10): words the caller must say to interrupt the assistant; 0 lets any speech do it.
- `keypadInputPlan`: with `{"enabled": true}`, keys the caller presses go to the model as what they typed, after a
  delimiter (`delimiters`, `#` by default) or `timeoutSeconds` (2) without a key.

Not used yet, and named in the call's warnings when set: `backoffSeconds`, `transcriptionEndpointingPlan`,
`smartEndpointingPlan` (the turn detector OpenRing always runs is Pipecat's smart turn model) and
`customEndpointingRules`.

### How fast the voice speaks

`voice.speed`, as on Vapi: 1 is normal, 1.2 a little faster. ElevenLabs takes 0.7 to 1.2, Deepgram 0.7 to 1.5, Kokoro
0.5 to 2 and OpenAI 0.25 to 4; a speed outside the range is brought to its edge. The other voices speak at their own
speed. The assistant editor has a slider.

### Names the transcriber should get right

Give it the words it will hear: names, brands, products. Use Vapi's fields, or **Words to recognize** in the assistant
editor:

```json
{"transcriber": {"provider": "deepgram", "model": "nova-3", "language": "pt-BR",
                 "keyterm": ["Clínica Sorriso", "Doutora Ana"]}}
```

`keyterm`, `keytermsPrompt` and `keywords` (one word, with an optional boost: `Sorriso:2`) are all accepted, up to 100
words of up to 50 characters, and go to the field each transcriber takes: Deepgram's nova-3 takes `keyterm` and refuses
`keywords`, and older models the reverse, so OpenRing sends the right one. Phrases don't fit Deepgram's older models.
Works on Deepgram, AssemblyAI, ElevenLabs, Cartesia's `ink-2` and Whisper; Gladia and Soniox ignore them, with a warning
in the log.

### Recording calls

With `"artifactPlan": {"recordingEnabled": true}`, or **Record calls** in the assistant editor, each call is saved
as a WAV file on your server, the caller on the left and the assistant on the right, about 2 MB per minute on the
phone and 6 MB in the browser. It's in
a `recordings` folder next to the database (`OPENRING_RECORDINGS_DIR` moves it). The call page plays it, and
`artifact.recordingUrl` is in the call and in the `end-of-call-report`. Fetching it needs your API key
(`GET /call/{id}/recording`), and `DELETE /call/{id}/recording` deletes it for good; both answer 409 while the call
is still going on. Recordings are deleted for good after 365 days: change it under **Settings > Recordings**, or
with `PATCH /settings/recordings {"retentionDays": 90}` (0 keeps them forever). While the assistant talks, its track can have gaps of a few hundredths of a second every so often; the call itself doesn't.

It's **off by default**, unlike on Vapi, where it's on. Many places require telling people they are being recorded,
and some require their consent, so say it in the greeting and check the rules where you and your callers are.
WAV is the only format for now: `recordingFormat` is ignored.

### Phone calls

Twilio has to reach your server, so it needs a public https URL. On your own computer, start a tunnel
(`cloudflared tunnel --url http://localhost:8000`), put its URL in `PUBLIC_URL` in `~/.openring/.env` and
restart. Then attach your Twilio number and point its **Voice webhook** (HTTP POST) at
`$PUBLIC_URL/twilio/voice`:

```bash
curl -X POST http://localhost:8000/phone-number \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"number": "+15551234567", "assistantId": "ASSISTANT_ID"}'
```

Call the number. Transcripts are at `GET /call` and on the **Calls** page.

To have the assistant call someone, send the same body as Vapi's `POST /call` (or use **Call a phone** in
the dashboard). Only call people who expect it: automated calls are regulated (the TCPA in the US, Anatel's
rules in Brazil). A Twilio trial account can only call numbers verified in its console.

```bash
curl -X POST http://localhost:8000/call \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"phoneNumberId": "PHONE_NUMBER_ID", "customer": {"number": "+5511912345678"}}'
```

**Voicemail.** With `voicemailDetection` or a `voicemailMessage` on the assistant, Twilio's answering
machine detection checks who picked up and waits for the beep. A voicemail gets the `voicemailMessage`, in the
assistant's voice (or a recording, when it's the URL of a .wav or .mp3), then the call hangs up with
`endedReason: "voicemail"`; without a message it just hangs up. It's off by default: a person hears the greeting
a few seconds later, and Twilio charges a small fee per call. `"voicemailDetection": "off"` turns it off.

```json
{"voicemailMessage": "Hi {{customer.name}}, this is Ava from the clinic. Please call us back.",
 "voicemailDetection": {"provider": "twilio", "beepMaxAwaitSeconds": 30}}
```

### Campaigns

A campaign has one assistant call a list of people, a few at a time, with the body of Vapi's `POST /campaign`.
Each person gets an ordinary outbound call that carries the `campaignId`, so transcripts and webhooks work as
for any call. In the dashboard, **Campaigns > New campaign** takes a pasted list or a CSV; the CSV's other
columns become variables.

```bash
curl -X POST http://localhost:8000/campaign \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Appointment reminders", "phoneNumberId": "PHONE_NUMBER_ID", "assistantId": "ASSISTANT_ID",
       "maxConcurrency": 3,
       "schedulePlan": {"earliestAt": "2026-11-03T12:00:00Z", "latestAt": "2026-11-03T20:00:00Z"},
       "customers": [{"number": "+5511912345678", "name": "Maria",
                      "assistantOverrides": {"variableValues": {"time": "3 pm"}}}]}'
```

The assistant's first message and instructions can use `{{customer.name}}`, `{{customer.number}}` and any key
of `variableValues`: `"firstMessage": "Hi {{customer.name}}, I'm calling to confirm your {{time}} visit."`.
`POST /call` takes `assistantOverrides` too. Calling starts at `earliestAt` (or right away), and no new call
starts after `latestAt`. `GET /campaign/{id}` has Vapi's counters (`callsCounterScheduled`,
`callsCounterEnded`, ...) and each call; `PATCH` with `{"status": "cancelled"}` stops it. A restarted server
picks its campaigns back up without calling anyone twice. To call again whoever didn't answer or was busy, add
`"retryPlan": {"maxAttempts": 3, "delayMinutes": 60}` (OpenRing's own field: up to 5 calls each, 5 to 1440
minutes apart; a voicemail or a failed call isn't retried, and nothing is called after `latestAt`). Workflows, squads and campaign webhooks are not
supported yet.

Only call people who agreed to get the call, at hours the law allows: automated calls are regulated (the
TCPA in the US, Anatel's rules in Brazil). The dashboard asks you to confirm that for every campaign.

### Tools

Tools use Vapi's format, so a server written for Vapi works unchanged. Put them in `model.tools`, or save
them once with `POST /tool` and list their ids in `model.toolIds`:

```json
"model": {
  "provider": "groq", "model": "openai/gpt-oss-20b",
  "tools": [
    {"type": "function",
     "function": {"name": "book_appointment", "description": "Book a cleaning",
                  "parameters": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}},
     "server": {"url": "https://your-app.com/tools"},
     "messages": [{"type": "request-start", "content": "One moment, let me check."}]},
    {"type": "endCall"},
    {"type": "transferCall", "destinations": [{"type": "number", "number": "+15551230000",
      "description": "front desk", "message": "Transferring you now."}]}
  ]
}
```

When the model calls `book_appointment`, OpenRing POSTs `{"message": {"type": "tool-calls", "toolCallList":
[...], "call": {...}}}` to the tool's `server.url` (or the assistant's) and reads back
`{"results": [{"toolCallId": "...", "result": "..."}]}`. `server.headers`, `server.secret`
(sent as `X-Vapi-Secret`), `server.timeoutSeconds`, `async` and the `request-start` / `request-failed`
messages work as on Vapi. `endCall` hangs up after the goodbye; `transferCall` moves a phone call to the
chosen number (Twilio) once the assistant stops talking. Tool calls and results show up in the call's
transcript.

**No server of your own? Use `apiRequest`.** The model fills in the `body`, and OpenRing sends it to your URL: an
n8n, Make or Zapier webhook, your CRM's API. There's no Vapi envelope, and the model reads the answer as it comes:

```json
{"type": "apiRequest", "name": "book_visit", "description": "Book a visit once the caller confirms the day",
 "method": "POST", "url": "https://hook.make.com/your-webhook",
 "headers": {"type": "object", "properties": {"Authorization": {"type": "string", "value": "Bearer YOUR_KEY"}}},
 "body": {"type": "object", "properties": {"day": {"type": "string", "description": "The day they chose"}},
          "required": ["day"]},
 "parameters": [{"key": "phone", "value": "{{customer.number}}"}],
 "timeoutSeconds": 20,
 "messages": [{"type": "request-start", "content": "One moment."},
              {"type": "request-response-delayed", "timingMilliseconds": 5000, "content": "Still checking."},
              {"type": "request-failed", "role": "system", "content": "Apologize, and don't promise it was booked."}]}
```

The URL, the headers and `parameters` are yours, not the model's, and `parameters` win over what it fills in: it can
only send the fields `body` declares, and a field with a `value` is fixed and hidden from it. They
can use `{{variables}}`, though not in the URL's host and not Liquid filters; a GET or DELETE carries the values
in its query. Vapi's `credentialId` isn't available: the key goes in `headers`, which stay on your server. A non-2xx
answer counts as a failure, and nothing is retried or redirected, so a booking isn't sent twice. Link-local and
cloud metadata addresses are refused; addresses on your own network work. The model reads the first 4,000
characters of the answer.

**Treat tool arguments as untrusted input.** The model chooses them during a conversation with whoever
called, and a caller can talk it into almost anything ("cancel every appointment", "my account number is
someone else's"). Check every argument on your server as you would a public web form: who the caller is
(`call.customer.number`), what they may see or change, and limits on how much. Set `server.secret` and verify
`X-Vapi-Secret` so only OpenRing reaches the tool. `transferCall` only ever dials the destinations you listed.

### Server messages

Your assistant's `server.url` gets Vapi's messages in Vapi's shape (`{"message": {"type": ..., "call": {...}}}`),
with `server.headers` and `server.secret` (as `X-Vapi-Secret`):

- `status-update` when the call starts (`in-progress`), is transferred (`forwarding`) and ends (`ended`, with
  `endedReason`);
- `conversation-update` after each answer, with the transcript so far;
- `speech-update` when the assistant or the caller starts or stops speaking (`status`, `role`, and `turn`, the number
  of exchanges so far);
- `user-interrupted` when the caller talks over the assistant;
- `hang` when the caller has waited 5 seconds for an answer;
- `end-of-call-report` with the transcript and per-turn latency.

Every message sent shows on the call's page, under **Webhooks**: the type, the server (without its query string), its
HTTP answer or why it failed, and how long it took (`GET /call/{id}/webhooks`, OpenRing's own). The log is kept in
memory, the last 2000 messages since OpenRing started, and never holds the payload or headers.

Pick which ones with `serverMessages`, as on Vapi. `transcript` is sent only when listed there: each thing the
transcriber hears (`transcriptType` `partial`, then `final`) and each thing the assistant says, with `role` and
`transcript`. List `transcript[transcriptType="final"]` instead to get the final ones alone, under that name. Messages
arrive in order; when your server is slow, partial transcripts are the ones left out. Not sent yet: `model-output`,
`voice-input` and `transfer-update`. A phone number without an assistant but with its own
`server.url` asks it on each incoming call (`assistant-request`) and uses the `assistantId` or the transient
`assistant` it answers. `POST /call` also takes a transient `assistant` instead of an `assistantId`.

### Recorded messages and on-call escalations

Beta, OpenRing's own, Twilio numbers only for now, and not yet tried on a real call. Upload a recording, WAV or MP3 up
to 10 MB and 10 minutes, and give it to an assistant. That assistant doesn't talk: when the call is answered it plays
the file and hangs up. With no transcriber, model or voice, the call costs only the line.

```bash
curl -H "Authorization: Bearer $OPENRING_API_KEY" -F file=@notice.wav http://localhost:8000/announcement
```

```json
{"name": "Maintenance notice", "announcement": {"audioId": "AUDIO_ID"}}
```

A campaign with that assistant plays the same message to everyone on its list. Twilio fetches the file from
`$PUBLIC_URL/announcement/play/<token>`, a random link made for one call that works for an hour; the recording's id is
never in it. `GET /announcement` lists the recordings; `DELETE /announcement/{id}` refuses while an assistant plays it.

An **escalation** calls an on-call list in order until someone presses the confirmation key, like an incident pager:

```bash
curl -X POST http://localhost:8000/escalation -H "Authorization: Bearer $OPENRING_API_KEY" \
  -H "Content-Type: application/json" -d '{"name": "Payments are down", "phoneNumberId": "NUMBER_ID",
  "assistantId": "PAGER_ASSISTANT_ID", "rounds": 2, "customers": [{"number": "+5511912345678", "name": "Ana"},
  {"number": "+5511987654321", "name": "Bruno"}]}'
```

Each person hears the recording, then "Press 1 to confirm." (`confirmDigit` and `confirmPrompt` change them), twice.
Whoever presses the key stops the escalation (`endedReason: "acknowledged"`, `acknowledgedBy`); otherwise it goes to
the next person, round the list `rounds` times (1 to 5), and ends as `nobody-confirmed`. `attempts` lists every call.
`PATCH /escalation/{id}` with `{"status": "cancelled"}` stops it. An escalation cut off by a restart is not called
again: it ends as `worker-shutdown`. Say in the recording what happened and which key confirms; the dashboard has an
**Escalations** page.

### Checked with Vapi's SDK

Vapi's official Python SDK (`vapi_server_sdk` 3.0.0), pointed at an OpenRing server, on November 13, 2026: **34 of the
38 calls we try pass.** The SDK parses every answer into Vapi's own types, so a pass means the JSON is what a Vapi client
expects. Run it yourself with `scripts/vapi_sdk_check.py`.

- Pass: assistants, tools, phone numbers, squads and files (create, list, get, update, delete); calls (list, filters,
  get, rename, delete); listing campaigns.
- Fail, because OpenRing doesn't have them: analytics, chats, sessions and the second version of knowledge bases
  (`/v2/knowledge-base`; the first, `/knowledge-base`, is in).
- Not tried: creating calls and campaigns (they would ring someone; the test suite covers them against simulated
  carriers), recording downloads, and Vapi's evals, simulations, scorecards and structured outputs, which OpenRing
  doesn't have.

### Memory per caller

Beta, and OpenRing's own: Vapi has no field for it. An assistant with `callerMemory` on is given, at the start of a phone
call, what was said on that number's last calls with it, so it can pick up where they left off:

```json
{"callerMemory": {"enabled": true, "calls": 3, "maxCharacters": 4000}}
```

`calls` is how many past calls it gets (1 to 10) and `maxCharacters` how much text in all (200 to 20000), split evenly
and keeping the end of each call. The memory is the transcripts OpenRing already keeps: nothing else is stored, no
model summarizes anything and nothing is charged. Delete a call (`DELETE /call/{id}`) and it is forgotten. Tools' requests and results are
left out. The call's record lists what it was given in `rememberedCallIds`. In a squad, each member with `callerMemory`
gets the number's last calls with that squad. Browser calls have no number, so no memory.

**A caller is known by the number the carrier reports, and that number can be faked.** Someone calling with another
person's number is told what that person said before. Leave it off where a past call may hold something private, or
have the assistant confirm who is calling before it mentions anything.

### Knowledge base

Upload files, and the assistant looks things up in them during a call. Same calls as Vapi: `POST /file`, then a `query`
tool, or **Knowledge** in the dashboard and the boxes in the assistant editor.

```bash
curl -H "Authorization: Bearer $OPENRING_API_KEY" -F file=@prices.pdf http://localhost:8000/file
```

```json
{"model": {"tools": [{"type": "query", "function": {"name": "faq"},
  "knowledgeBases": [{"name": "faq", "description": "Prices and opening hours", "fileIds": ["FILE_ID"]}]}]}}
```

PDF, .docx, text, Markdown, CSV, JSON, YAML, XML and logs, up to 20 MB each. A scanned PDF has no text to read, a PDF
that takes over 3 minutes to read is refused, and old .doc files need saving as .docx. Only the text is kept, in
OpenRing's database.

The search runs on your server, for free, in a few milliseconds and with nothing to download. It **matches words, not
meanings**: the model picks the words, and tries others when nothing comes back, and a short file goes to the model
whole, so it can read what the search can't. For long documents, say in the instructions when to search and use the
files' own terms. For search by meaning, use your own: `provider: "custom-knowledge-base"` with a `server.url`, as a
`knowledgeBases` entry or through `POST /knowledge-base` and `model.knowledgeBaseId`. It gets Vapi's
`knowledge-base-request` and answers `documents` or a `custom-message`. Unlike on Vapi, where it's asked on every turn,
the model asks it when it needs to, and the message also has the `query`.

The import copies `query` tools, but not their files: upload them again and list the new ids.

### Squads

Several assistants on one call, each with its own instructions, tools, model and voice: one answers and hands the caller
to a specialist, who has the conversation so far. Same JSON as Vapi, in `POST /squad`, or **Squads** in the dashboard:

```json
{"name": "Front desk", "members": [
  {"assistantId": "RECEPTION_ID", "assistantDestinations": [
    {"type": "assistant", "assistantName": "Billing", "description": "the caller asks about an invoice",
     "message": "One moment, I'll pass you to billing."}]},
  {"assistantId": "BILLING_ID"}
]}
```

The first member answers. Put `squadId` on a phone number or in `POST /call` (or an inline `squad`), and an
`assistant-request` can answer with either. A `handoff` tool does the same as `assistantDestinations` with more control,
and works on an assistant with no squad; whoever it names by `assistantId` comes along to the call:

```json
{"type": "handoff", "function": {"name": "to_billing", "parameters": {"type": "object",
   "properties": {"reason": {"type": "string"}}}},
 "destinations": [{"type": "assistant", "assistantName": "Billing",
   "contextEngineeringPlan": {"type": "lastNMessages", "maxMessages": 10}}]}
```

- What the model fills in (`reason`) becomes a variable: the next assistant's prompt can say `{{reason}}`.
- `contextEngineeringPlan` is what the next assistant is given: `all` (the default), `lastNMessages`,
  `userAndAssistantMessages`, `previousAssistantMessages` or `none`. The call's record always keeps everything.
- On taking over, it follows its own `firstMessageMode`: it says its `firstMessage`, or, with none, answers with the
  model. `assistant-waits-for-user` waits for the caller.
- A tool still running at a handoff finishes for the record, and no model answers it.
- The call's `handoffs` lists each change. `membersOverrides`, a member's `assistantOverrides` and `tools:append` work.

The call's limits, idle messages, recording and webhooks come from the first assistant. If an assistant's providers all
fail, another's take over rather than leaving the caller in silence. The switch waits for the assistant to finish
talking, about a second of quiet. Not available yet: `dynamic` destinations, handing off to a whole squad,
`variableExtractionPlan`, rejection plans, and squads in campaigns. Assistants with different transcribers or voices
keep a connection open to each during the call.

### SIP trunks

Any carrier that speaks SIP, with no Twilio in between, so you pay the carrier's price. OpenRing speaks SIP and RTP
(G.711, over UDP) itself. Same JSON as Vapi, or **Phone numbers > SIP trunks** in the dashboard:

```bash
curl -X POST http://localhost:8000/credential -H "Authorization: Bearer $OPENRING_API_KEY" \
  -H "Content-Type: application/json" -d '{"provider": "byo-sip-trunk", "name": "My carrier",
    "gateways": [{"ip": "sip.carrier.example"}], "outboundLeadingPlusEnabled": true,
    "outboundAuthenticationPlan": {"authUsername": "USER", "authPassword": "PASSWORD"}}'

curl -X POST http://localhost:8000/phone-number -H "Authorization: Bearer $OPENRING_API_KEY" \
  -H "Content-Type: application/json" -d '{"provider": "byo-phone-number", "number": "+15551234567",
    "credentialId": "CREDENTIAL_ID", "assistantId": "ASSISTANT_ID"}'
```

Calls to the number reach the assistant, `POST /call` and campaigns dial out through the trunk, and `transferCall`
sends a SIP REFER; if the other line doesn't pick up, the caller goes back to the assistant. A carrier that wants registration gets it with `sipRegisterPlan: {"domain": "sip.carrier.example"}`
in the authentication plan. `numberE164CheckEnabled: false` allows an extension like `1000`, and `customer.sipUri`
calls a `sip:` address directly. The trunk's login goes only to its own gateways, so an address elsewhere that asks for
one is refused.

**The carrier has to reach your computer by UDP**, on port 5060 and on the audio ports 40000-40999, so this needs a server
with a public address or port forwarding: a web tunnel won't do. `SIP_PORT` and `SIP_RTP_PORTS` change them, and
behind a NAT `SIP_PUBLIC_IP` says what address to give the carrier.

Nothing listens until a trunk is saved. SIP servers on the internet get probed all day by people looking to make calls on
someone else's bill, so OpenRing answers only the gateways of your trunks and whoever is on a call with it, and ignores
everyone else without a word; a softphone on your network works by listing its address as a gateway. The password goes in and never comes back
through the API. It's kept in OpenRing's database, and SIP over UDP isn't encrypted, as is usual with carriers.

**With your carrier.** Whoever it is, it comes down to the same fields. SIP over UDP has to be on, with G.711:

| OpenRing | What it is at the carrier |
|---|---|
| `gateways[].ip` | The SIP server or termination address it gives you, like `sip.telnyx.com` or `sip.zadarma.com` |
| `authUsername`, `authPassword` | The trunk's SIP login. A carrier that goes by IP instead needs your public address on its allow-list |
| `sipRegisterPlan` | Only if it asks you to register, as many that give you a login do |
| Where to send calls | Your public address, port 5060, which the carrier calls the origination URI or the like |

The same goes for a PBX like Asterisk or FreePBX: add a SIP trunk pointing at OpenRing, and list the PBX's address as a
gateway.

Not available yet: SIP over TCP or TLS, voicemail detection, and codecs other than G.711. On Twilio or Telnyx, SIP
domains and trunks already reach their webhooks: the number in the `sip:` address finds the assistant, and
`customer.sipUri` dials out through Twilio. No real carrier has carried a call through this yet, so please tell us how
it goes.

### Provider fallback

Same fields as Vapi. When a provider fails during a call in a way a retry won't fix (a rejected key, no
credit left, the provider down), the next one in the list takes over for the rest of the call. Unlike Vapi,
a fallback model can come from another provider:

```json
"transcriber": {"provider": "deepgram", "fallbackPlan": {"transcribers": [{"provider": "assembly-ai"}]}},
"model": {"provider": "groq", "model": "openai/gpt-oss-20b",
          "fallbackModels": ["openai/gpt-oss-120b", {"provider": "openai", "model": "gpt-4.1-mini"}]},
"voice": {"provider": "cartesia", "voiceId": "YOUR_VOICE_ID",
          "fallbackPlan": {"voices": [{"provider": "kokoro", "voiceId": "af_heart"}]}}
```

When the model switches, the new one answers the caller's last message, so the turn isn't lost. A fallback
whose key is missing is skipped, and each switch is listed in the call's `providerFallbacks` and on its page.

### Model routes

A model route is a saved list of models, in order, that several assistants can share, like OmniRoute's
combos. Make one on the **Model routes** page (or `POST /model-route`) and pick it in the assistant editor
(`"model": {"routeId": "ROUTE_ID", ...}`; the assistant keeps its own instructions and tools):

```json
{"name": "Fast, then cheap", "preset": "fastest",
 "models": [{"provider": "groq", "model": "openai/gpt-oss-20b"},
            {"provider": "openrouter", "model": "openai/gpt-4.1-mini"},
            {"provider": "ollama", "model": "llama3.2"}]}
```

A call starts on the first model in the route's order. The next one takes over when a model fails (a rejected
key, no credit left, a rate limit, an outage) or when it hasn't started answering after `maxLatencyMs`: the
slow request is cancelled and the next model answers the same turn. The model that let the route down rests
for `cooldownMinutes`, so new calls start on the next one until it's back.

The `preset` sets the order and those two numbers. OmniRoute's combos offer many presets and sixteen scoring
sliders, for a gateway that serves coding tools; a phone call needs three things (answer fast, keep going,
cost little), so OpenRing keeps three presets, plus Custom for everything:

| Preset | Order | Switches after | Rests a model |
|---|---|---|---|
| `fastest` | The quickest measured answer first | 1.5 s | 5 min |
| `cheapest` | The lowest price first, local models before paid ones | 3 s | 5 min |
| `reliable` | Yours, as listed | 2 s | 15 min |
| `custom` | `"order"`: `listed`, `fastest` or `cheapest` | `"maxLatencyMs"` (or `null`: errors only) | `"cooldownMinutes"` |

"Fastest" goes by how long each model took to start answering on your recent calls; **Measure speed** on the
route's page (or `POST /model-route/{id}/measure`) asks each model for one short answer to time it, a few
tokens each. "Cheapest" goes by list prices (see [What a call costs](#what-a-call-costs)). Models not
measured or priced yet come after the others, in your order. Changing the order or a number on a preset
route makes it `custom`. Without a route, `"maxLatencyMs"` on an assistant's `model` does the same for its
`fallbackModels`.

A route can also hold **transcribers** or **voices**: `POST /model-route` with `"kind": "transcriber"` (or
`"voice"`) and members like `{"provider": "deepgram", "model": "nova-3"}` or `{"provider": "11labs", "voiceId": "..."}`,
then `"transcriber": {"routeId": "ROUTE_ID"}` (or `"voice"`) on the assistant. The first member answers, the others
take over in order when it fails, and one that failed rests for `cooldownMinutes` across calls, so the next calls start on
the next one. They are ordered as listed or by price (`"order": "cheapest"`, per minute of audio or per 1M characters;
free local ones first). There is no `fastest` order and no latency limit for them, because nothing measures how fast a
transcriber or a voice answers. What the assistant's `transcriber` or `voice` says besides the provider (language,
keyterms, speed) applies to every member. In the dashboard, the Model routes page has a button for each kind, and the
assistant editor offers a route for the transcriber and for the voice. `GET /model-route?kind=voice` lists one kind.

### What a call costs

Every call records what its providers charge, at list prices, in Vapi's fields: `cost` (US$) and
`costBreakdown.llm`, `.stt`, `.tts`, `.line` and `.total`, next to the tokens, voice characters and seconds of
audio it used. A call that switched providers is priced per provider. The dashboard shows it per call and per minute.
`line` is the phone line (browser calls have none). Twilio's is priced for US and Canadian numbers: local and
toll-free inbound, outbound, each with Media Streams, rounded up to the minute like Twilio bills it. Telnyx, Plivo,
Vonage, SIP trunks and other countries have no shipped price: the call names `phone line (<carrier>)` in `unpriced`
until you add a rate below.

Model prices ship with OpenRing (`openring/llm_prices.json`, from [LiteLLM](https://github.com/BerriAI/litellm)'s
price list and OpenRouter's model list); speech-to-text and voice prices come from each provider's pricing
page (`openring/prices.py`). Local providers cost nothing. A provider without a known price is listed in
`costBreakdown.unpriced` and left out of the total, never guessed. For your own rates, or a model the list
doesn't have, write `~/.openring/prices.json` and restart OpenRing:

```json
{"llm": {"custom-llm": {"my-model": [0.2, 0.6]}},
 "stt": {"deepgram": {"nova-3": 0.0065}},
 "tts": {"rime-ai": {"*": 30}},
 "line": {"telnyx": {"inbound": 0.01, "outbound": 0.01}}}
```

Models are per 1M tokens (input, output), speech-to-text per minute, voices per 1M characters; `"*"` is any
model of that provider. A line rate is per minute for `inbound`, `inbound-tollfree` or `outbound`; add a prefix
(`"outbound:+55"`) to limit it to numbers that start so. `GET /prices` shows every price in use.

### Telnyx

Calls through Telnyx are new and not yet confirmed on a real Telnyx call. In the Telnyx portal,
create a **TeXML Application** whose voice URL is `$PUBLIC_URL/telnyx/voice` (HTTP POST) and assign your
number to it. Add the number in OpenRing with `"provider": "telnyx"`, and set `TELNYX_API_KEY` and
`TELNYX_PUBLIC_KEY` (Keys & Credentials > Public Key), or connect Telnyx from the Providers page. Every
webhook is checked against that public key.

To call out, name the TeXML application: `TELNYX_APPLICATION_ID`, or `texmlApplicationId` on the number. `POST /call` and
campaigns then work as on Twilio. `transferCall` works too, by giving the live call a `<Dial>` through Telnyx's
TeXML "Update call" request, with the account id Telnyx sends in each webhook; it follows Telnyx's documentation but hasn't been
run on a real Telnyx call. Voicemail detection isn't available on Telnyx yet.

### Plivo

Not yet confirmed on a real Plivo call. In the Plivo console, create an **Application** whose Answer URL is
`$PUBLIC_URL/plivo/voice` and Hangup URL `$PUBLIC_URL/plivo/status` (both HTTP POST) and assign your number to it. Add
the number in OpenRing with `"provider": "plivo"`, and set `PLIVO_AUTH_ID` and `PLIVO_AUTH_TOKEN`, or connect Plivo from
the Providers page. Every webhook's signature is checked with that token. Calls in and out, campaigns and `transferCall`
work; voicemail detection isn't available on Plivo yet.

### Vonage

Not yet confirmed on a real Vonage call. In the Vonage dashboard, create a **Voice application** with signed webhooks on,
Answer URL `$PUBLIC_URL/vonage/voice` and Event URL `$PUBLIC_URL/vonage/event`, and link your number to it. Add the number
in OpenRing with `"provider": "vonage"`, and set `VONAGE_APPLICATION_ID`, `VONAGE_PRIVATE_KEY` (the path of the file
Vonage lets you download) and `VONAGE_SIGNATURE_SECRET` (Settings > API settings), or connect Vonage from the Providers
page. Calls in and out, campaigns and `transferCall` work, with audio at 16 kHz; voicemail detection isn't available on
Vonage yet.

### WhatsApp calls

Beta and **unofficial**, never tried on a real call. An assistant answers and places WhatsApp voice calls through
[Wavoip](https://wavoip.com), a service that links a WhatsApp account as a "device" and carries its calls. It is not
Meta's Calling API:

- WhatsApp doesn't allow unofficial clients, and may restrict or ban a number used this way. Use a number you can lose.
- The call's audio goes through Wavoip's servers, and Wavoip has its own plans and limits.
- OpenRing speaks the protocol of Wavoip's open-source browser client; Wavoip can change it without notice.

In Wavoip's panel, create a device, link the WhatsApp account (QR code) and copy the device's token. Then add the
number, here or under **Phone numbers** in the dashboard (provider **WhatsApp**):

```bash
curl -X POST http://localhost:8000/phone-number \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"provider": "wavoip", "number": "+5511999990000", "wavoipToken": "DEVICE_TOKEN", "assistantId": "ASSISTANT_ID"}'
```

OpenRing connects to the device from your server, so it needs no `PUBLIC_URL` and no tunnel. A WhatsApp call to the
number is answered by its assistant or squad; `POST /call` and campaigns with this `phoneNumberId` ring people on
WhatsApp. The token is never shown again: the number comes back with `wavoipTokenSet`, `wavoipConnected`,
`wavoipStatus` (`ready`, or what is missing) and `wavoipLinkedNumber`. A call that isn't picked up ends as
`customer-busy`, `customer-did-not-answer` or `wavoip-failed-to-connect-call`.

Not there: `transferCall` (a WhatsApp call can't be moved to a phone line), voicemail detection, and devices of
Wavoip's official kind (Meta's Calling API over WebRTC), whose calls are left ringing. Wavoip charges per device, not
per minute, so the line shows as `phone line (wavoip)` in a call's `unpriced`. WAHA can tell that a call is coming and
reject it, but can't carry its audio (its docs, November 2026), which is why this goes through Wavoip.

### Genesys Cloud

If your contact center runs on Genesys Cloud, an assistant can take calls inside your Architect flows,
through Genesys's **Audio Connector** (the AudioHook protocol). Not yet confirmed on a real Genesys call.

1. On the **Providers** page, connect **Genesys Cloud**: press **Generate** for an API key and a client
   secret, copy both, and save. (Or set `GENESYS_API_KEY` and `GENESYS_CLIENT_SECRET`; the secret is base64.)
2. In Genesys, add an **Audio Connector** integration with the base connection URI
   `wss://YOUR_PUBLIC_HOST/ws/genesys` (shown on the **Phone numbers** page) and those two credentials.
3. In Architect, add a **Call Audio Connector** action. Pass the assistant as the input variable
   `assistantId`, or add the called number in OpenRing with `"provider": "genesys"` and its assistant. Every
   other input variable fills the assistant's `{{variables}}` (`{{accountId}}`, say); the caller's name
   fills `{{customer.name}}`.
4. After the action, the output variable `action` says what happened: `finished`, or `transfer` with the
   number in `transferTo` (from a `transferCall` tool), so the flow can route the call. `callId` points to
   the call in OpenRing.

Every connection must carry the API key and Genesys's signature made with the client secret; anything else
is refused. Transcripts, latency and webhooks work as for any other call.
