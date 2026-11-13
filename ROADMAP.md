# Roadmap

Where OpenRing is heading. Priorities change with what people need, so open an issue to argue for
something, or pick an item and say you're on it. Shipped work moves to the [changelog](CHANGELOG.md).

## Next, in this order

1. **Proof before features.** Every beta carrier and feature on a real call (Telnyx, Plivo, Vonage, SIP, outbound,
   campaigns, transfers), an import from a real Vapi account, and the official Vapi SDK run against OpenRing, with the
   coverage published. [Testing on a real call](https://github.com/elifalanrocha/openring/labels/needs-testing) is the
   most useful thing anyone can do.
2. **The rest of Vapi's server messages and tools:** `model-output`, `voice-input`, `transfer-update`, and tools from an
   MCP server. `speech-update`, `user-interrupted`, `hang` and `transcript` are in.
3. **Memory per caller:** the assistant remembers who called before, kept on your server, at no charge.
4. **Calls over WhatsApp**, incoming first.
5. **Answering other AI agents.** More and more callers are assistants calling for a person: notice it, answer
   briefly and in a form a program can read, and mark those calls. Then an open way for two agents to recognize each
   other on a call and finish in seconds.
6. **An MCP server,** so any AI agent can place and follow a phone call through OpenRing.
7. **Speech-to-speech models** (OpenAI Realtime, Gemini Live), next to the transcriber, model and voice stack.
8. **Load tests,** with the number of simultaneous calls a small server takes, published.

## Later

- **Knowledge base by meaning:** embeddings next to the word search.
- **Campaigns:** calling hours per time zone and Vapi's campaign webhooks.
- **Squads:** destinations chosen by your server, handing off to a whole squad, and squads in campaigns.
- **Agencies:** several clients on one server, each with its own assistants, numbers, limits and branding.
- **Scale:** Postgres.
- **Dashboard in Portuguese and Spanish.**

## Waiting for someone to ask

Open an issue if you need one of these: more carriers in the cost per call (Telnyx, Plivo, Vonage, other countries),
Exotel and outbound calls through Genesys Cloud, SIP over TCP and TLS, voicemail detection beyond Twilio, faster local
speech-to-text with Moonshine, and desktop installers.
