# Migrating from Vapi to OpenRing

OpenRing speaks Vapi's API, so most of a migration is copying your assistants over and changing one base URL. This guide
walks through it in the order you would do it, says what comes across unchanged, what changes, and what does not exist
here yet. "Vapi" is named only to say what is compatible with what; OpenRing is an independent project.

It was written from a real migration: an account with two assistants and one tool was imported and an assistant from it
took a call in the browser the same day.

## What you get, and what you give up

- **No platform fee.** OpenRing runs on your machine or server with your own provider keys, so you pay the providers
  (transcriber, model, voice, phone line) and nothing per minute to OpenRing. The dashboard of the account we migrated
  priced one assistant at about **US$0.08 a minute on Vapi, US$0.05 of it hosting**. The same assistant on OpenRing,
  with Deepgram, a Groq model and the free local voice, is estimated at **about US$0.008 a minute**. Those are different
  models and a different voice, so the quality is not the same: the number says what the platform costs, not that the
  result is equal.
- **Your data stays with you.** Calls, transcripts and recordings are in a SQLite file on your own disk.
- **What you give up for now:** Vapi's own hosted voices and the "Composer", "Boards" and organizations screens. See
  [What is not here](#what-is-not-here).

## 1. Run OpenRing

```bash
npx openring          # installs what it needs (uv, Python) and opens the dashboard
```

The first run creates `~/.openring/.env` with an API key for the REST API, and the dashboard asks you to choose a
password. Provider keys
(Deepgram, Groq, ElevenLabs, Twilio...) go on the **Providers** page, or in that file.

## 2. Import your account

Press **Import** on the Assistants page, or call the API:

```bash
curl -X POST http://localhost:8000/import \
  -H "Authorization: Bearer $OPENRING_API_KEY" -H "Content-Type: application/json" \
  -d '{"baseUrl": "https://api.vapi.ai", "apiKey": "YOUR_VAPI_PRIVATE_KEY"}'
```

Use the account's **private** key. It is used once to read your account and never stored. Nothing is changed or deleted on
the Vapi side: the import only reads.

What comes across, with its full JSON (a field OpenRing does not use yet stays in the document and starts working when
support lands):

| Copied | Notes |
|---|---|
| Assistants | Prompt, first message, end-call phrases, `serverMessages`, analysis plan, start-speaking plan, everything |
| Tools | `function`, `apiRequest`, `endCall`, `transferCall`, `query`, `handoff`, `mcp`; the assistant's `toolIds` are pointed at the copies |
| Squads | Members and handoffs, pointed at the copied assistants |
| Structured outputs | And the assistants' `structuredOutputIds` |
| Twilio, Telnyx and Vonage numbers | Without their secrets (see step 5) |

What does not come across: ids and organization fields (the copies get their own), secrets (a Twilio auth token, a tool's
`credentialId`: put the key where the tool sends it), Vapi's own phone numbers and SIP trunks, call history.

Importing again **updates** what came before instead of making copies, so you can import, change things on Vapi, and
import once more.

### Read the report

The answer lists each assistant with anything that keeps it from taking calls here yet. In the account we migrated it
said the same thing for both assistants:

> the vapi voice Elliot is not available here, so Kokoro (free, on this computer) speaks instead; pick another voice
> (Elliot is an Inworld voice: provider inworld, voiceId Elliot, with your Inworld key)

Vapi's own voices are served by Vapi and cannot be called from outside it, so the importer puts the free local voice
(Kokoro) in their place, keeps the original in `importedVoice`, and tells you. Everything else on the assistant is
untouched.

## 3. Check what each assistant needs

**Put your provider keys in first** (Providers page), then import: a transcriber or model whose provider has no key here
cannot take a call, so the importer swaps it for one that has a key, and says so in the report. A transcriber goes to
the first of Deepgram, AssemblyAI, Gladia, Soniox, Cartesia, OpenAI or Groq you have a key for; a model goes to
`openai/gpt-oss-120b` on Groq, or Cerebras, or the same model through OpenRouter. The original is kept in
`importedTranscriber` and `importedModel`. Nothing that already works is touched, and where no provider has a key the
assistant is left as it was, so add a key and import again.

Then open each assistant, look at the three stages (transcriber, model, voice), and check the model answers the way you
want: an open model is not the model you had. The model picker marks models of providers without a key as "needs a key".

### Choosing a model

The **Choose** button next to the model field opens one list of every provider's models, with a search box, a provider
filter and sorting by name, low latency or low cost per minute. Each row shows the time to the first word and the model's
cost per minute at list prices. "Speed not measured" means exactly that: OpenRing shows a time only where it has one, from
its own test calls, from this server's calls, or from **Measure**, which times one short answer on your account now.
There is no "intelligence" score, because there is no honest source for one.

## 4. Test a call in the browser

Open the assistant, the **Test** tab, **Talk to it**. This needs no phone line and no tunnel, so do it first: it proves
the transcriber, model and voice work with your keys before a phone is involved.

## 5. Phone numbers

Imported Twilio numbers come without their auth token. Put `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in the environment
or on the Providers page. Then:

1. Phone providers cannot reach `localhost`. Give OpenRing a public address: a server, or a tunnel while you test
   (`cloudflared tunnel --url http://localhost:8000`), and put it in `PUBLIC_URL`.
2. In Twilio, set each number's **A call comes in** webhook to `$PUBLIC_URL/twilio/voice` (HTTP POST).
3. Call the number.

Outbound calls (`POST /call`) and campaigns use the same line. A Twilio **trial** account cannot place them to your own
verified phone from a number you bought (error 573003), so test outbound on a paid account. Telnyx, Plivo, Vonage, Genesys
and SIP trunks are set up on the Phone numbers page; see the [API guide](api.md#phone-calls).

## 6. Point your code at OpenRing

If your code calls Vapi's API (or uses Vapi's SDK), change the base URL to your server and use your OpenRing API key:

```python
from vapi import Vapi
client = Vapi(token="YOUR_OPENRING_API_KEY", base_url="https://your-openring.example.com")
```

We ran Vapi's official Python SDK against an OpenRing server: **34 of the 38 calls we try pass**, and the SDK parses every
answer into Vapi's own types, so the JSON is what a Vapi client expects. What fails because it does not exist here:
analytics, chats, sessions, and the second version of knowledge bases. The full list is in the
[API guide](api.md#checked-with-vapis-sdk), and `scripts/vapi_sdk_check.py` reruns it.

### Webhooks

Your server receives Vapi-shaped messages (`{"message": {"type": ..., "call": {...}}}`), at the assistant's `server.url`.
By default OpenRing sends `status-update`, `conversation-update`, `speech-update`, `user-interrupted`, `hang`,
`tool-calls`, `transfer-destination-request` and `end-of-call-report`. These go **only to an assistant that lists them** in
`serverMessages`: `transcript` (all, or `transcript[transcriptType="final"]`), `model-output` (the model's text as it is
written), `voice-input` (the text handed to the voice) and `transfer-update`. If your server relies on one of these,
list it on the assistant after importing.

### Tools

`function`, `apiRequest`, `endCall`, `transferCall`, `query` (knowledge bases) and `handoff` work as in Vapi. An `mcp` tool
works with an MCP server that speaks the Streamable HTTP transport: at the start of a call OpenRing asks the server for its
tools and gives each to the model. A server that only offers the older SSE transport gives no tools, and the call goes on
without them.

## Differences worth knowing

These are places where OpenRing's default differs from Vapi's, or a field is read differently. None is a surprise if you
know it:

- **Off by default here, on in Vapi:** the post-call analysis (`analysisPlan`), the office background sound on phone calls,
  recording. Add the field to turn it on.
- **Words to recognize:** `keywords` and `keyterm` go to the field each transcriber and model accept (Deepgram rejects
  `keywords` on nova-3), and a transcriber that cannot use them gets a warning on the assistant.
- **A call whose model, transcriber or voice stops working** ends with `pipeline-error` and the reason (a rejected key, no
  credit) unless a fallback is set, rather than staying silent.

## What is not here

Said plainly, so you can decide before you move:

- **Vapi's hosted voices** (the `vapi` voice provider). Use any of the voices OpenRing supports (ElevenLabs, Cartesia,
  Deepgram, OpenAI, Rime, Inworld, Kokoro and others).
- **Composer, Boards, organizations and members** screens, and Vapi's analytics, chats and sessions APIs.
- **Proof on real phone lines.** Inbound calls on Twilio have been confirmed on real calls. Outbound calls, campaigns,
  transfers, Telnyx, Plivo, Vonage, SIP, WhatsApp and Genesys are built and tested against simulated carriers, and are marked
  beta until someone runs them on a real line.

## Rolling back

Nothing was removed from your Vapi account, so there is nothing to undo: point your code and your phone webhooks back where
they were.
