# Changelog

All notable changes to OpenRing are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A new identity, in the family of Real Valor, which makes OpenRing: a symbol of two half-discs of a ring pulled apart
  (one ring, two voices), black, white and greys with OpenRing's orange as its one signal, and the Archivo typeface. The
  dashboard takes it: flat surfaces with 1px rules, corners of 4 to 6px, no shadows. Logo files in `docs/assets/brand/`.
- Calls share the voice activity and turn detectors' models instead of loading them again each: a new call no longer
  stalls the others for about 300 ms, and 20 calls at once take 368 MB instead of 956 MB.
- `scripts/load_test.py` measures how many calls at once a computer takes (about 20 per process on a 2018 laptop; the
  numbers are in `docs/deploy.md`).
- `openring mcp`: an MCP server, so an AI agent can place a call through OpenRing and read the transcript.
- Callers that are AI agents ("I'm an AI assistant calling on behalf of…", in English, Portuguese and Spanish) are
  noticed, the call is marked `callerIsAI`, and the model is told to answer briefly and exactly (`aiCallerPlan`).
- The dashboard moves more smoothly: switching assistants keeps the list still and brings in only the editor, cost and
  latency bars grow into place, and controls answer the pointer; all of it off with reduced motion.
- A webhook log: each call's page lists the messages it sent to your server, with the answer and the time each took
  (`GET /call/{id}/webhooks`).
- Vapi's `startSpeakingPlan.waitSeconds`, `stopSpeakingPlan.voiceSeconds` and `numWords`, and `keypadInputPlan`: keys the
  caller presses reach the model as text. Settings with no equivalent yet are named in the call's warnings.
- Write an assistant from a description: on a new assistant, a model you connected drafts its name, greeting and
  system prompt (`POST /assistant/draft`, OpenRing's own); nothing is saved until you save it.
- Recorded messages (beta, Twilio): upload a WAV or MP3 (`POST /announcement`) and give it to an assistant
  (`announcement.audioId`); the call plays it and hangs up, costing only the line. A campaign plays it to a whole list.
- On-call escalations (beta, Twilio, `/escalation`, an **Escalations** page): call a list in order until someone presses
  the confirmation key, round the list up to 5 times.
- Assistants open beside their list: each with its model's logo and its three layers, the first one opened at once
  with its cost, answer time and setup. "Instructions" is now "System prompt", the name the field has everywhere else.
  Pages come in with a short, quiet movement (none with reduced motion).
- The dashboard serves its own typeface (Archivo, SIL OFL), so it no longer calls Google Fonts.
- Vapi's live server messages, in Vapi's shape: `speech-update` (who started or stopped speaking, with the `turn`),
  `user-interrupted` (the caller talked over the assistant) and `hang` (the caller has waited 5 seconds for an answer),
  sent by default as on Vapi; and `transcript`, partial and final, for an assistant that lists it in `serverMessages`
  (`transcript[transcriptType="final"]` for the final ones alone). A server that falls behind loses partial
  transcripts, never anything else. Tested on simulated calls only.
- Calls over WhatsApp (beta, unofficial, never tried on a real call): a phone number with `"provider": "wavoip"` and a
  `wavoipToken` answers and places WhatsApp voice calls with an assistant through Wavoip, with no public URL. The token is
  never shown by the API; the number says how its device is (`wavoipStatus`). WhatsApp may ban a number used through an
  unofficial client: the docs and the dashboard say so.
- `voice.speed`, as on Vapi, on ElevenLabs, OpenAI, Deepgram and Kokoro voices, kept within what each takes (a
  warning says when a speed was limited or a voice can't change it), with a slider in the assistant editor.
- The assistant editor opens with what the assistant costs per minute and how long it takes to answer, layer by layer:
  measured on its own last calls, or the cost estimated from list prices until it has some
  (`GET /assistant/{id}/profile`, OpenRing's own).
- A new server starts with an assistant to talk to, made once from what costs nothing: Whisper and Kokoro on this
  computer, and Ollama's model when Ollama is running (Groq's otherwise, which needs its free key).
- The dashboard's menu folds to its icons (a button by the logo, remembered on this browser) and scrolls without
  showing a bar.
- `PATCH /call/{id}` renames a call and `DELETE /call/{id}` deletes it for good, record, transcript and recording, as
  on Vapi; a call still going on is refused.
- `scripts/vapi_sdk_check.py` runs Vapi's official Python SDK against an OpenRing server: 34 of the 38 calls it tries
  pass on `vapi_server_sdk` 3.0.0 (the list is in `docs/api.md`).
- Memory per caller (beta, OpenRing's own field): with `"callerMemory": {"enabled": true}` an assistant is given, when a
  phone call starts, what was said on that number's last calls with it (`calls`, 3 by default, and `maxCharacters`,
  4000). It reads the transcripts already on your server: nothing else is stored, no model is asked and nothing is
  charged, and a deleted call is forgotten. Off by default, because a caller ID can be faked. A "Remember callers" box
  in the assistant editor; the call records which calls it was given in `rememberedCallIds`.

## [0.1.7] - 2026-11-11

### Added

- A Docker image, `ghcr.io/elifalanrocha/openring`, published on every release (amd64), and a deploy guide
  (`docs/deploy.md`) with a Render blueprint (`render.yaml`) and a Railway config (`railway.json`).
- Campaigns call again whoever didn't answer or was busy: `retryPlan` with `maxAttempts` (up to 5 calls each) and
  `delayMinutes` (5 to 1440, 60 by default), a "Tries each" field on the Campaigns page, and `callsCounterRetrying`.
  Each call in a campaign records its customer's position (`index`) and `attempt`.
- Routes of transcribers and voices (`"kind": "transcriber"` or `"voice"` on `POST /model-route`, used with
  `transcriber.routeId` or `voice.routeId`): in order or by price, failing over and resting after a failure like model
  routes. The Model routes page has a button for each kind and the assistant editor picks a route for the transcriber and the
  voice. `GET /model-route?kind=`.
- `transferCall` on Telnyx (beta): the live call is given a `<Dial>` with TeXML's Update call, using the account id Telnyx sends
  with each webhook.
- The Docker image keeps the downloaded Kokoro and Whisper models on the `/data` volume (`HOME=/data`), so a new container
  doesn't fetch them again.
- On Render, Railway and Fly.io, `PUBLIC_URL` defaults to the address the platform gives the service.

### Fixed

- Installing on Python 3.13 works again: `audioop-lts` is a declared dependency there (SIP's audio needs it, and Pipecat 1.16
  stopped bringing it, which made every import of the server fail).
- OAuth sign-ins survive a restart: the key that encrypts them is made once and kept in the settings file
  (`OAUTH_ENCRYPTION_KEY`), not generated anew at every start.

## [0.1.6] - 2026-11-09

### Added

- The phone line in a call's cost (`costBreakdown.line`, included in `total`): Twilio's list prices for US and Canadian
  numbers, rounded up to the minute. Other carriers and countries are named in `unpriced` until you add a rate in
  `~/.openring/prices.json` (`"line"`).
- The Calls page exports to CSV, with formula cells made safe for spreadsheets, and a call's page copies its transcript
  (thanks to Vaibhav R Valakunde, vaibhavrvalakunde2006-gif).
- Provider logos on the Providers page.
- OmniRoute takes an API key on the Providers page (`OMNIROUTE_API_KEY`), for when its API key protection is on;
  it is checked against OmniRoute's model list before it's saved.
- OAuth sign-in (`/oauth/*`) for xAI, Anthropic and OpenAI with the server owner's own OAuth client
  (`<PROVIDER>_CLIENT_ID`). OpenRing never signs in as another vendor's app. OpenRouter keeps its own sign-in.

- Calls in and out through Plivo and Vonage, and out through Telnyx (beta): signed webhooks, campaigns, and
  `transferCall` on Plivo and Vonage. Connect them on the Providers page. The import brings Telnyx and Vonage numbers.
- `openring --version` (thanks to Vaibhav R Valakunde, vaibhavrvalakunde2006-gif) and `openring --host`, which wins over
  `HOST` and warns in its help that anything but localhost exposes the dashboard (thanks to Arin Aeruva, arin30).
- Vapi's `createdAtGt`, `createdAtLt`, `createdAtGe` and `createdAtLe` on `GET /call`, `/assistant`, `/tool` and
  `/phone-number`, filtered in SQL, in any ISO 8601 offset, with a 400 for a date that isn't one (thanks to
  k_s_neeraj, Neeraj111010). Importing from another OpenRing with more than 100 items now gets every page.
- **Disconnect** on the Providers page asks for a second click, labeled **Confirm disconnect**, before it removes a key
  (thanks to Arin Aeruva, arin30).
- Prices for Rime's Coda and Smallest.ai's Lightning voices, and `"*"` in `~/.openring/prices.json` now covers every
  model of a provider, including the ones with a price of their own (thanks to Mohit Anand, coughlmao).
- Squads (beta), in Vapi's format: several assistants on one call, each with its own prompt, tools, model and voice.
  `/squad`, the `handoff` tool and `assistantDestinations`, `squadId` on numbers, calls and `assistant-request`,
  `contextEngineeringPlan`, `firstMessageMode` and `tools:append`; the call's `handoffs` lists each change. The import
  brings squads.
- Knowledge bases (beta), in Vapi's format: `POST /file`, the `query` tool, `/knowledge-base` and
  `model.knowledgeBaseId`. The search runs on your server with SQLite's full-text index, matching words, or asks your
  own with `custom-knowledge-base`. PDFs are read with pypdf, a new dependency, in a process of their own with a time
  limit.
- SIP trunks (beta), in Vapi's format: `byo-sip-trunk` credentials in `/credential` and `byo-phone-number` numbers, in and
  out, with login, registration and transfers, over UDP with G.711. Only your trunks' gateways get an answer.
  `customer.sipUri` works, and Twilio and Telnyx SIP calls find their number.
- Words the transcriber should get right, in Vapi's `transcriber.keyterm`, `keytermsPrompt` and `keywords`, or
  **Words to recognize** in the assistant editor. Each goes to the field its transcriber and model take, since Deepgram
  refuses a connection given the wrong one.
- Call recording, in Vapi's `artifactPlan.recordingEnabled`: a stereo WAV per call on your server, played on the
  call's page, with `artifact.recordingUrl` on the call and in the `end-of-call-report`.
  `GET /call/{id}/recording` needs the API key or a dashboard session, and `DELETE` removes the file. It's off by
  default, unlike on Vapi: many places require telling people they are being recorded. Recordings are deleted after
  365 days; change it under **Settings > Recordings** or with `PATCH /settings/recordings` (0 keeps them for good).

- Call limits, in Vapi's fields: a call ends by itself after `maxDurationSeconds` or after `silenceTimeoutSeconds`
  without activity (the clock waits while someone talks, the model thinks or a tool runs), with Vapi's `endedReason`
  (`exceeded-max-duration`, `silence-timed-out`),
  saying `endCallMessage` first. When the caller goes quiet after the assistant spoke,
  `messagePlan.idleMessages` are said ("Are you still there?"), after `idleTimeoutSeconds`, up to
  `idleMessageMaxSpokenCount` times. The assistant editor has the length, the silence and the idle message.
  These limits apply to assistants you already have: give `maxDurationSeconds` a higher value if their calls can run
  past 10 minutes. `endCallMessage` is also said when an `endCall` tool has no message of its own, as on Vapi.

- Voicemail detection on outgoing calls (beta), in Vapi's fields: with `voicemailDetection` or a `voicemailMessage`,
  Twilio's answering machine detection checks who picked up and waits for the beep. A voicemail gets the message, in
  the assistant's voice (or a recording, from the URL of a .wav or .mp3), and the call ends with `endedReason:
  "voicemail"`, which campaigns count in `callsCounterEndedVoicemail`; without a message it just hangs up. It stays
  off unless asked for. The assistant editor has the switch and the message.

- The `apiRequest` tool, in Vapi's format: the model fills in the `body` and OpenRing sends it to your URL, such as
  an n8n, Make or Zapier webhook, with no server of your own. The URL, `headers` and `parameters` stay yours and
  can use `{{variables}}`; the model can only send the fields `body` declares. Only a 2xx answer counts; nothing is
  retried or redirected, a caller's interruption doesn't
  cancel a request already sent, and cloud metadata addresses are refused. Vapi's `credentialId` isn't available.
- Tools: `request-response-delayed` messages are said when a tool runs late, and a `request-failed` message with
  `"role": "system"` now goes to the model instead of being read out to the caller.

### Changed

- Calls now have Vapi's default limits: 10 minutes at most, and 30 seconds of silence from the caller. Before,
  a call with nobody on the line kept running (and billing the providers) until the phone company dropped it.

### Fixed

- Only OpenAI is sent the `developer` message role now. Pipecat adds such messages during a call (a tool's late answer, a
  caller's empty turn), and a provider that refuses the role ended the call. Mistral, DeepSeek and Perplexity go through
  Pipecat's own services for them, which follow their rules for messages.
- A caller speaking during a transfer no longer cancels it, and a transfer that fails now gets an answer: the model tells
  the caller, or the tool's `request-failed` message does, instead of leaving them in silence.
- When a provider stopped working during a call (a rejected key, no credit left, an outage) and no fallback could
  take over, the caller heard silence until they hung up, and the call was recorded as `customer-ended-call`,
  a success. The call now ends right away with `pipeline-error: the voice (11labs ...) stopped working: ...`,
  naming the layer, the provider and its error.

- A Vonage webhook token that carries a payload hash now vouches for that body only. Before, a captured token also
  passed with no body at all, so it could stand in for any GET to `/vonage/event` or `/vonage/voice` for five minutes.
  An outgoing Vonage call that was answered but never connected its websocket now ends with
  `vonage-failed-to-connect-call` instead of staying `queued`.
- OAuth's `redirect_uri` has to be on the same origin as `PUBLIC_URL` (or on localhost). It was a prefix match, so
  `https://your-host.evil.example` passed.

## [0.1.5] - 2026-11-01

### Added

- The new-campaign form warns when calls could reach people before 8 AM or after 9 PM their time, the hours the
  TCPA allows in the US. Each number's time zone comes from its country code (and its area code in Brazil and
  North America); a campaign with no stop time is checked for how long its list may take.

### Fixed

- A campaign created through the API with the same number twice (written the same way or not, in one list
  or across `dialPlan` entries) called that person twice; repeats are now left out. Customer numbers in
  campaigns and `POST /call` need 7 to 15 digits, as saved phone numbers do, instead of any digit at all.
- With its output going to a pipe (Docker logs, systemd, `openring > log`), `openring` printed nothing at
  startup: Python held the lines in a buffer while the server ran. They now show right away, and the Docker
  image sets `PYTHONUNBUFFERED`.
- When an example API key (`change-me`) was ignored on a first start, nothing said so: the generated key took
  over silently and clients still sending the example got 401. `openring` now says the example is ignored and
  where the key in use is.
- Ten wrong passwords on the server's own computer locked its owner out of the dashboard for five minutes,
  even with the right password. Sign-ins from this computer are no longer slowed down; through a tunnel or
  the network, each address still is.
- Right after a release, `npx openring` could answer that the new version doesn't exist: uv still held an older
  copy of PyPI's index. The launcher now asks PyPI again, once, when that happens (without slowing down or
  breaking offline starts the rest of the time).
- A Genesys connection whose signature carried a time that isn't a number crashed with an error in the log
  instead of being refused.
- Deleting a model route left its assistants pointing at it: their calls used the assistant's own model, as
  intended, but any change to them, even a new name, was refused with "model route not found". Deleting a
  route now moves its assistants back to their own model.
- **Measure speed** failed with an error 500 when one of the route's models broke a rule added after it was
  saved (such as a refused `model.url`); that model now shows why, and the others are measured.
- Dates in the dashboard followed the browser's language ("30 de out.") on an English page; they now match
  the page. A call's cards come in rows of even length (4 + 3 rather than 5 + 2), wide enough for their values
  and labels, and a call with one tool call no longer says "1 tool calls".
- On the overview, the bars under **Busiest assistants** and **How calls ended** moved with the length of each
  row's label and number; they now start and end at the same place in every row. The overview's cards fill
  whole rows (3 per row, 5 on wide screens), so a label keeps its traffic light on one line from phones to wide
  screens, and more end reasons read in words ("Assistant ended the call", "Transferred", "Server restarted").

### Security

- URLs that come from assistants, phone numbers and API calls (tool servers, `server.url`, `custom-llm`
  `model.url`, the import source) could point at a cloud server's metadata address (169.254.169.254 and the
  like). Whoever could set one could then read the machine's cloud credentials through OpenRing: a tool's
  answer lands in the transcript, and the import report repeats what the source sent. Those addresses
  (link-local and cloud metadata, IPv4 and IPv6) are now refused, for every request and redirect; local and
  private networks stay allowed.
- When a tool failed, the error the model read (and could say to the caller) included the tool's full URL,
  which often carries a secret (webhook paths). The model now gets the reason without the URL, and logs show
  only the scheme and host of webhook URLs.

## [0.1.4] - 2026-11-01

### Changed

- Incoming calls through Twilio are no longer beta: confirmed on a real phone call (a caller in Brazil to a US
  number, with Deepgram, Groq and the Kokoro voice), including interruptions and the cost record. Outgoing calls
  through Twilio, and so campaigns, stay beta until a real one is confirmed.

### Fixed

- The local Whisper transcriber didn't start on Apple Silicon Macs (M1 and later) unless MLX Whisper was
  installed: Pipecat requires it just to load its Whisper module there, though OpenRing uses faster-whisper.
- Lists (calls, assistants...) created in the same instant could come back in either order; the newest is
  now always first.
- Phone numbers are checked when saved (`POST`/`PATCH /phone-number`): 7 to 15 digits, with the country
  code. Text without digits used to match any other such text, so an import could skip a real number and
  an anonymous caller could reach a number saved by mistake; the importer now skips non-numbers with a
  reason. The Providers page no longer scrolls sideways on a phone when the settings path is long.

### Security

- An example API key (`change-me` from `.env.example`, which the Docker setup copied) was accepted, so a
  server deployed that way answered anyone who knew the example. Example keys are now refused: `openring`
  replaces one with a generated key, saved so it stays the same across restarts, and the dashboard shows it
  under Settings. `.env.example` no longer carries one.
- A server without an API key answered API requests from anywhere. Its API now answers only the computer
  it runs on (not tunnels or the network); the dashboard keeps working through its sign-in.

## [0.1.3] - 2026-11-01

### Added

- Route presets, OmniRoute's mode packs cut down to what a call needs: **Fastest** (the model with the
  quickest measured answers first), **Cheapest** (the lowest price first, local models before paid ones),
  **Reliable** (your order, longer rest for a failing model) and **Custom** (`order`, `maxLatencyMs` and
  `cooldownMinutes`, all yours). Changing a setting on a preset route makes it Custom.
- Model speed: each call records how long its model took to start answering, and
  `POST /model-route/{id}/measure` (**Measure speed** on the route's page) times one short answer from each
  model. The route editor shows each model's speed and price, and the order the next call will try.
- Cost per call in dollars, at list prices: `cost` and `costBreakdown.llm`, `.stt`, `.tts`, `.total` (Vapi's
  fields), priced per provider when a call switched. Model prices ship in `openring/llm_prices.json` (from
  LiteLLM's price list and OpenRouter's), speech-to-text and voices in `openring/prices.py`;
  `~/.openring/prices.json` overrides them and `GET /prices` lists them. The dashboard shows the cost per
  call, per minute and in total.
- Model routes (beta), like OmniRoute's combos: a saved list of models, in order, that assistants share
  (`/model-route`, the **Model routes** page, and a route picker in the assistant editor). The next model
  takes over when one fails or, with `maxLatencyMs`, when it is slow to start answering: the slow request is
  cancelled and the next model answers the same turn. A model that let the route down rests for
  `cooldownMinutes`, so new calls start on the next one. Each switch shows its reason on the call's page.
- Fixed: after a model switch, the replaced model is stopped and the response it left open is closed, so the
  caller hears one answer and the new model's reply reaches the transcript.
- A 100% local, free stack: the new `whisper` transcriber (faster-whisper) runs on your computer, with
  Ollama for the model and Kokoro for the voice, so nothing is billed per minute and the audio stays on the
  machine. **Use the free local stack** in the assistant editor sets all three; the editor offers the models
  pulled into Ollama, and the Providers page says whether Ollama is running. Whisper's model (`tiny` to
  `large-v3-turbo`, `base` by default) downloads once in the background when an assistant uses it, is shared
  by every call, runs on a GPU when there is one, and gets phone audio at the 16 kHz it expects.
  `npx openring` now installs it.
- Genesys Cloud (beta): an assistant takes calls inside your Architect flows through Genesys's Audio Connector
  (`/ws/genesys`, the AudioHook protocol). Each connection must carry the API key and Genesys's HMAC
  signature made with the client secret, which the Providers page can generate. The flow picks the assistant
  with an `assistantId` input variable (or the called number does); other input variables fill the
  assistant's `{{variables}}`. Output variables hand the call back: `action` is `finished`, or `transfer`
  with `transferTo`.
- Campaigns (beta), with Vapi's `/campaign` API: one assistant calls a list of people from a Twilio number,
  `maxConcurrency` at a time, between `schedulePlan.earliestAt` and `latestAt`, with Vapi's counters and
  `PATCH {"status": "cancelled"}` to stop. A restarted server picks its campaigns back up without calling
  anyone twice. The new **Campaigns** page takes a pasted list or a CSV (its extra columns become variables),
  shows who was called and how each call ended, and asks you to confirm that everyone agreed to be called.
- Variables and `assistantOverrides`, as on Vapi: the first message and instructions can use
  `{{customer.name}}`, `{{customer.number}}` and any key of `variableValues`, on campaigns and `POST /call`.
- Callers in any language: with `"language": "multi"` the model is told to answer in the caller's language and
  to switch when they do. The assistant editor offers it as **Follow the caller**, next to common languages,
  and warns when the chosen voice speaks only one language (Kokoro, Deepgram); ElevenLabs and OpenAI voices
  read any.
- Provider fallback (beta), in Vapi's fields: `transcriber.fallbackPlan.transcribers`, `model.fallbackModels`
  and `voice.fallbackPlan.voices`. When a provider fails in a way a retry won't fix (a rejected key, no
  credit, an outage, a rate limit), the next one takes over for the rest of the call. A fallback model can be
  on another provider (`{"provider": "openai", "model": "gpt-4.1-mini"}`), and when the model switches the new
  one answers the caller's last message. Each switch is recorded in the call's `providerFallbacks` and shown
  on its page.

## [0.1.2] - 2026-10-30

### Added

- The Providers page shows what's left on each connected account that reports it: dollars on OpenRouter,
  Deepgram, Twilio and Telnyx, characters on ElevenLabs, with a warning when it runs low.
- What each call used, in Vapi's `costBreakdown` fields: `llmPromptTokens`, `llmCompletionTokens` and
  `ttsCharacters`, plus `transcriberAudioSeconds`. `performanceMetrics.firstWordLatency` is how long the
  caller waited for the greeting.
- The overview shows the wait for the first word, tokens per call, the 95th-percentile response time and
  how many calls were transferred. Success rate, response time and first word get a traffic light (good,
  fair, poor) based on what callers expect, for example 90% or more of calls ending normally and replies
  within 1.2 s.
- Requests to OpenRouter carry OpenRing's name (`HTTP-Referer` and `X-Title`), so OpenRouter can list
  OpenRing among the apps that use it. Nothing else is sent; `OPENRING_ATTRIBUTION=false` turns it off.
- Incoming calls through Telnyx (beta): point a TeXML Application's voice URL at `/telnyx/voice`. Webhooks are
  checked against the account's Ed25519 public key (`TELNYX_PUBLIC_KEY`); `TELNYX_API_KEY` lets OpenRing
  hang up. Telnyx can be connected from the Providers page. Outbound calls and transfers still go through Twilio.

### Changed

- New look: a ring-and-wave logo, an orange palette in light and dark themes, and a quieter dashboard.
  Numbers are colored only when they need attention, labels are no longer in spaced capitals, and the
  fee you didn't pay leads the overview.
- Recent calls on the overview are links, so they work from the keyboard.
- A rewritten README, and [PRINCIPLES.md](PRINCIPLES.md): what OpenRing promises and how it decides what to build.

## [0.1.1] - 2026-10-30

### Changed

- Releases on npm are staged by CI and approved by a maintainer with two-factor authentication.

### Fixed

- A call whose caller hung up in its first seconds, while its speech, model and voice services were still
  starting, stayed "in-progress" for good; so did a call whose provider failed to start. Both now end, with
  `customer-ended-call` or `pipeline-error: ...`.

## [0.1.0] - 2026-10-30

The first public release. Everything below is new.

### Voice agents

- Vapi-compatible REST API for assistants (`/assistant`), phone numbers (`/phone-number`), calls
  (`/call`) and tools (`/tool`): existing Vapi clients work by changing the base URL.
- Inbound phone calls through Twilio, and outbound calls with `POST /call`.
- Talk to an assistant from the browser, with no phone number needed.
- Assistant tools in Vapi's format: `function`, `endCall` and `transferCall`.
- Import assistants, tools and phone numbers from a Vapi-compatible API.
- Per-turn latency on every call, in Vapi's `artifact.performanceMetrics` shape, and Vapi-style
  `end-of-call-report` webhooks.

### Providers

- Models from any OpenAI-compatible provider: OpenAI, Groq, OpenRouter, DeepSeek, OmniRoute or a
  custom URL.
- Speech to text with Deepgram; voices from ElevenLabs or Kokoro, a free voice that runs on your
  computer.
- Connect providers from the dashboard: each key is checked with the provider before it's saved, and
  OpenRouter offers one-click sign-in.

### Dashboard

- Overview with calls, minutes, success rate, response time and the platform fee you didn't pay.
- Assistants, phone numbers, calls with transcripts and latency, providers and settings, in light and
  dark themes.
- Sign-in with a password; the API keeps its Bearer key.

### Running it

- One command: `npx openring` installs everything, including Python, and opens the dashboard.
- Docker image and `docker-compose.yml`.
- Twilio webhooks are rejected unless their signature checks out.
