# Principles

What OpenRing promises the people who run it, and how we build it. When a feature, a pull request or a
provider deal conflicts with one of these, the principle wins. Changing a principle takes a public
discussion in an issue, not a quiet commit.

## What we promise

1. **No platform fee, ever.** OpenRing never charges per minute and never sits between you and your
   providers' bills. You pay Deepgram, OpenAI, Twilio or whoever you choose, directly, at their price.
2. **Your keys, your calls, your server.** Keys stay in `~/.openring/.env` on your machine. Calls and
   transcripts stay in your database. There is no telemetry: apart from downloading its own software and
   the free voice model, OpenRing only talks to the providers and webhooks you configure.
3. **Vapi-compatible.** An existing Vapi client works by changing the base URL. We use Vapi's JSON shape
   and provider names, and we add fields instead of renaming them, so nothing that works today breaks
   tomorrow.
4. **One command to a first call.** `npx openring` works on an ordinary laptop with nothing installed
   but Node.js. The default stack includes a free local voice, so the first test call costs nothing.
5. **Any provider, no lock-in.** Every layer (telephony, speech-to-text, model, voice) can be swapped.
   Defaults are picked for quality, latency and price. A provider that sponsors OpenRing is labeled as a
   sponsor and never becomes a default because it pays.
6. **Honest about what works.** The README says what works and what doesn't yet. A feature is
   "supported" only after a real call has gone through it; until then it is labeled beta.
7. **Fast enough for a phone call.** Latency is a feature. Every call records per-turn latency, and a
   change that makes calls slower needs a good reason.
8. **Safe by default.** Webhooks are verified, the dashboard has a password, and when OpenRing can't check
   something it refuses rather than guesses.

## How we build it

- **Ship small and often.** A focused pull request merged today beats a big one next week. Every release
  goes to PyPI and npm together, with the same version number and a changelog entry.
- **Every feature has a test.** Tests run without paid services, keys or a network, and pass on Linux,
  Windows and macOS. `pytest` and `ruff check .` pass before anything is pushed.
- **Keep the stack small.** Python and [Pipecat](https://github.com/pipecat-ai/pipecat) on the server, one
  dashboard file with no build step. A new dependency needs a reason and a license compatible with MIT,
  and goes into [THIRD_PARTY.md](THIRD_PARTY.md).
- **No secrets in git.** The pre-commit hook and CI both check for keys and `.env` files.
- **English in code, commits and docs**, so anyone can contribute. Translations are welcome as extra files.

## Should we build it?

A new feature should do at least one of these, and break none of the promises above:

- make calls cheaper per minute;
- make OpenRing easier to install, run or migrate to;
- make it more compatible with Vapi clients;
- make real calls more reliable or faster.

Big ideas that do none of these, like a visual flow builder or our own models, wait. Integrations with
tools people already use (n8n, CRMs, calendars) come through [Vapi-style tools](docs/api.md#tools) instead.
