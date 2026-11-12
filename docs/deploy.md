# Deploying OpenRing on a server

OpenRing ships as a Docker image, `ghcr.io/elifalanrocha/openring`, with Whisper and the Kokoro voice inside.
Tags: `latest`, a version (`0.2.0`) and a minor (`0.2`). Every server needs three things:

| Setting | Why |
| --- | --- |
| `OPENRING_PASSWORD` | The dashboard password. Nobody opens the dashboard on the server itself, so you can't choose it on first visit. |
| `PUBLIC_URL` | The https address phone providers reach. Render, Railway and Fly.io set it for you; elsewhere write it yourself. |
| A volume at `/data` | The database, recordings, the generated keys and the downloaded voice and speech models (about 120 MB for the free voice). Without it, every deploy starts empty and downloads them again. |

Provider keys (Twilio, Deepgram, Groq...) can be connected on the dashboard's Providers page afterwards.
The API key is under Settings, or in `/data/.env` if you didn't set `OPENRING_API_KEY`.

## Any server with Docker

```bash
docker run -d --name openring --restart unless-stopped -p 8000:8000 -v openring-data:/data \
  -e OPENRING_PASSWORD='choose a long one' -e PUBLIC_URL=https://voice.example.com \
  ghcr.io/elifalanrocha/openring:latest
```

Put a reverse proxy with HTTPS in front (Caddy: `voice.example.com { reverse_proxy localhost:8000 }`). Twilio and
the browser's microphone both need https. The image is built for `amd64`.

## Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/elifalanrocha/openring)

`render.yaml` makes a web service from the Dockerfile with a 1 GB disk and a generated API key; Render asks for
`OPENRING_PASSWORD`. The service needs a paid plan: disks aren't offered on the free one, and a free service
sleeps, which would make a call's webhook wait.

## Railway

New project, **Deploy from GitHub repo**, pick your fork of this repository (`railway.json` points at the
Dockerfile). Then in the service:

1. Variables: `OPENRING_PASSWORD`.
2. Settings, Networking: **Generate Domain** (this sets `PUBLIC_URL` automatically after the next deploy).
3. Settings, Volumes: add a volume mounted at `/data`.

## Status

The image builds and answers `/health` in CI on every push. The Render and Railway files follow those platforms'
documentation but haven't been run on a real account yet; if something is off, please open an issue.
