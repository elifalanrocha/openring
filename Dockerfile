FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY openring ./openring
RUN pip install --no-cache-dir ".[kokoro,whisper]"

# HOME is the volume too: the voice and speech models download to ~/.cache, and a new container must not fetch them again.
ENV OPENRING_DB_PATH=/data/openring.db HOST=0.0.0.0 OPENRING_HOME=/data HOME=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000
CMD ["openring"]
