# Third-party software

OpenRing's own code is released under the [MIT License](LICENSE). It runs on open-source libraries and
models made by other people, listed here with their licenses. OpenRing does not copy their code into
this repository: `pip`, `uv` or `npx openring` download them from PyPI when you install, and the voice
model downloads from GitHub on first start. The [Docker image](Dockerfile) is the exception: it bundles
everything below, so whoever redistributes that image also redistributes these components.

Versions are the ones tested for this release; licenses come from each package's metadata or repository.

## Copyleft components (read this if you redistribute OpenRing)

The free Kokoro voice is optional (the `openring[kokoro]` extra, which `npx openring` and the Docker
image install). It turns text into phonemes with eSpeak NG, which is GPL-licensed:

| Component | License | Used for |
| --- | --- | --- |
| [eSpeak NG](https://github.com/espeak-ng/espeak-ng), bundled as a shared library by [espeakng-loader](https://github.com/thewh1teagle/espeakng-loader) (loader: MIT) | GPL-3.0-or-later | Phonemes for the Kokoro voice |
| [phonemizer](https://github.com/bootphon/phonemizer) 3.4 | GPL-3.0-or-later | Calls eSpeak NG for Kokoro |
| [num2words](https://github.com/savoirfairelinux/num2words) 0.5 | LGPL-2.1 | Spells out numbers for Kokoro |
| [soxr](https://github.com/dofuuz/python-soxr) 1.0 | LGPL-2.1-or-later | Audio resampling |
| [PyAV](https://github.com/PyAV-Org/PyAV) 17 (its wheels bundle FFmpeg libraries) | BSD-3-Clause; bundled FFmpeg: LGPL-2.1-or-later | Audio for browser test calls |
| FFmpeg from Debian (Docker image only) | LGPL-2.1-or-later and GPL-2.0-or-later parts | Audio tools in the container |

OpenRing uses these as separate programs and libraries, unmodified. If you distribute a build that
contains them (for example, the Docker image), include their license texts and make their source code
available, as those licenses require. Their sources are at the links above. To run without any GPL
component, install `openring` without the `kokoro` extra and use a cloud voice such as ElevenLabs.

## Models

| Model | License | Notes |
| --- | --- | --- |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad (int8 ONNX export and voices from [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx/releases)) | Apache-2.0 | Downloaded on first start to `~/.cache/pipecat/kokoro-onnx` |
| [Silero VAD](https://github.com/snakers4/silero-vad) | MIT | Detects when the caller speaks; ships inside Pipecat |
| [Whisper](https://github.com/openai/whisper) by OpenAI, as CTranslate2 conversions by [Systran](https://huggingface.co/Systran) (and [deepdml](https://huggingface.co/deepdml/faster-whisper-large-v3-turbo-ct2) for large-v3-turbo) | MIT | Only when an assistant uses the `whisper` transcriber: downloaded from Hugging Face to `~/.cache/huggingface` |

## Data

| Data | License | Notes |
| --- | --- | --- |
| Model prices from [LiteLLM](https://github.com/BerriAI/litellm)'s `model_prices_and_context_window.json` | MIT | Copied into `openring/llm_prices.json` by `scripts/update_prices.py` |
| Model prices from [OpenRouter](https://openrouter.ai)'s public model list | Public API data | Same file, for the `openrouter` provider |

## Fonts

The dashboard loads [IBM Plex Sans and IBM Plex Mono](https://github.com/IBM/plex) (SIL Open Font
License 1.1) from Google Fonts; the font files are not part of OpenRing.

## Python libraries

Direct dependencies:

| Package | Version | License |
| --- | --- | --- |
| [pipecat-ai](https://github.com/pipecat-ai/pipecat) | 1.15 | BSD-2-Clause |
| [fastapi](https://github.com/fastapi/fastapi) | 0.162 | MIT |
| [uvicorn](https://github.com/encode/uvicorn) | 0.54 | BSD-3-Clause |
| [httpx](https://github.com/encode/httpx) | 0.28 | BSD-3-Clause |
| [python-multipart](https://github.com/Kludex/python-multipart) | 0.0.32 | Apache-2.0 |
| [aiortc](https://github.com/aiortc/aiortc) | 1.15 | BSD-3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | 50.0 | Apache-2.0 or BSD-3-Clause |
| [pypdf](https://github.com/py-pdf/pypdf) | 6.2 | BSD-3-Clause |
| [audioop-lts](https://github.com/AbstractUmbra/audioop) (Python 3.13 and later only) | 0.2 | PSF-2.0 |
| [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (`kokoro` extra) | 0.6 | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`whisper` extra, through Pipecat's) | 1.2 | MIT |

Installed with them (through Pipecat's Deepgram, ElevenLabs, OpenAI, Silero, WebSocket, runner and Whisper
extras, and aiortc):

| License | Packages |
| --- | --- |
| MIT | agent-detector, annotated-doc, annotated-types, anyio, attrs, charset-normalizer, ctranslate2, deepgram-sdk, dlinfo, docopt, docstring-parser, fastapi-cli, fastapi-cloud-cli, fastar, filelock, h11, h2, hpack, httptools, hyperframe, ifaddr, jiter, loguru, loudness, markdown-it-py, mdurl, onnxruntime, pydantic, pydantic-core, pydantic-extra-types, pydantic-settings, pyee, PyYAML, rich, rich-toolkit, rignore, sentencex, sentry-sdk, truststore, typer, typing-inspection, urllib3, watchfiles, win32-setctime |
| MIT-0 | cffi |
| MIT-CMU | pillow |
| BSD-2-Clause | pipecat-ai-prebuilt, Pygments |
| BSD-3-Clause | aioice, av, click, cloudpickle, colorama, fsspec, httpcore, httpcore2, httpx2, idna, itsdangerous, Jinja2, joblib, Markdown, MarkupSafe, numba, protobuf, pycparser, pylibsrtp, python-dotenv, soundfile, starlette, websockets |
| BSD-3-Clause and others | numpy (BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0) |
| BSD-2-Clause and Apache-2.0 with LLVM exception | llvmlite |
| Apache-2.0 | aiofiles, aiosignal, flatbuffers, frozenlist, google-crc32c, googleapis-common-protos, hf-xet, huggingface-hub, multidict, openai, opentelemetry-api, opentelemetry-exporter-http-transport, opentelemetry-exporter-otlp-common, opentelemetry-exporter-otlp-proto-common, opentelemetry-exporter-otlp-proto-http, opentelemetry-proto, opentelemetry-sdk, opentelemetry-semantic-conventions, propcache, pyOpenSSL, requests, tokenizers, yarl |
| Apache-2.0 and MIT | aiohttp, sniffio |
| Apache-2.0 or BSD | cryptography, packaging |
| ISC | dnspython, resampy, shellingham |
| PSF-2.0 | aiohappyeyeballs, typing-extensions |
| MPL-2.0 | certifi |
| MPL-2.0 and MIT | tqdm |
| Unlicense | email-validator |
| 0BSD | detect-installer |

The npm launcher (`bin/openring.js`) uses only Node.js built-ins and installs
[uv](https://github.com/astral-sh/uv) (MIT or Apache-2.0), which provides Python.

To refresh this list after changing dependencies, read the license metadata of the installed packages,
for example with `pip-licenses --with-urls`.
