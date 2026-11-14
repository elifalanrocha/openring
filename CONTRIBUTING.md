# Contributing to OpenRing

Thanks for helping. OpenRing is a self-hosted voice AI agent platform with a Vapi-compatible API, so the
most useful contributions make it easier to run, cheaper per minute, or more compatible with existing
Vapi clients. Small pull requests get merged fastest. [PRINCIPLES.md](PRINCIPLES.md) says what we
promise users and how we decide what to build.

New here? Look for issues labeled
[`good first issue`](https://github.com/elifalanrocha/openring/labels/good%20first%20issue).

## Set up

You need Python 3.11 or later and, to try the npm launcher, Node.js 18 or later.

```bash
git clone https://github.com/elifalanrocha/openring
cd openring
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,kokoro,whisper]"
pytest                             # a few minutes; includes a simulated Twilio call and a local WebRTC call
python -m openring                 # dashboard at http://localhost:8000
```

A change to how calls run (turn-taking, the pipeline, a provider) also gets a real call:
`python scripts/smoke_call.py` places one through your own keys (Deepgram and Groq; `--voice provider:voiceId` for a
voice other than Kokoro), over a simulated phone line. It calls nobody and costs a few cents.

The first start downloads the free Kokoro voice (about 120 MB). Provider keys go in `~/.openring/.env`
or are added from the dashboard's Providers page; see [.env.example](.env.example).

## Where things are

| Path | What it does |
| --- | --- |
| `openring/api.py` | FastAPI app: the Vapi-style REST API, Twilio webhooks, the media websocket and the dashboard routes |
| `openring/bot.py` | One live call: the Pipecat pipeline transport → speech-to-text → model → voice |
| `openring/providers.py` | Maps a Vapi assistant JSON to providers; add an OpenAI-compatible model provider with one line in `LLM_PROVIDERS` |
| `openring/keys.py` | Connects providers from the dashboard: checks a key with the provider, then saves it |
| `openring/store.py` | SQLite document store for assistants, phone numbers, calls and tools |
| `openring/static/index.html` | The whole dashboard: one file, vanilla JavaScript, no build step |
| `bin/openring.js` | The `npx openring` launcher |
| `tests/` | pytest suite; every feature has a test here |

## Rules of the road

- **Keep the Vapi JSON shape.** A new field uses Vapi's name and structure when Vapi has one. Prefer
  adding fields over renaming them, so existing clients keep working.
- **Every change comes with a test**, and `pytest` and `ruff check .` must pass. CI runs both on
  Linux, Windows and macOS.
- **Never commit keys, tokens or `.env` files**, not even in tests: use obviously fake values.
  Run `git config core.hooksPath .githooks` once, and a pre-commit check stops you if you try; CI runs
  the same check.
- **No paid service is required to run the tests.** Mock provider HTTP calls (see `tests/test_keys.py`).
- **Say "Vapi-compatible"**, and don't use Vapi's name or logo in any other way.
- Lines are up to 120 characters; `ruff check . --fix` sorts imports.

## Pull requests

1. Open an issue first for anything larger than a bug fix, so we can agree on the approach.
2. Branch from `main`, keep the change focused, and describe what it does and how you tested it.
3. Fill in the pull request template; a maintainer reviews within a few days.

By contributing, you agree that your work is released under the [MIT License](LICENSE).

## Security problems

Please don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
