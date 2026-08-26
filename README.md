# InternetBoard v1.0 Production

InternetBoard is a local research agent and long-term knowledge system for a QNAP TS-673A class NAS.

Production profile in this package:

- Host: QNAP TS-673A, AMD Ryzen Embedded V1500B, 40 GB RAM, NVIDIA GTX 1650 4 GB.
- Deployment root: `/share/Container/internetboard`.
- Runtime: Docker Compose / QNAP Container Station.
- AI: exactly one model, `qwen3.8:27b-q4_K_M`, served by `ollama/ollama:0.32.13`.
- Inference profile: one parallel request, one loaded model, 8192 context, Flash Attention, q8_0 KV cache, automatic CPU/RAM + NVIDIA partial GPU offload.
- Web port: `8788` by default.

## Install

QNAP prerequisites:

1. Container Station is installed and Docker Compose is available.
2. The QNAP NVIDIA driver/NvKernelDriver packages are installed.
3. The GTX 1650 is assigned to Container Station.
4. At least 35 GB RAM and 35 GB free space under `/share/Container` are available.
5. The NAS can reach Docker registries, Ollama model storage, and the public web sources you want to research.

Extract this package anywhere under a QNAP share, enter the extracted directory, and run:

```sh
sh install.sh
```

`install.sh` copies the release to `/share/Container/internetboard` if necessary, preserves an existing `.env`, validates Compose, builds the application images, verifies NVIDIA visibility, pulls the exact Qwen3.8 model, runs a schema-constrained AI request, verifies actual GPU participation, and checks the final HTTP health endpoint. It stops with a non-zero exit code if a production requirement is not met.

After a successful install:

```text
http://NAS-IP:8788
```

The generated API key is stored in `.env` and printed once at the end of installation. The browser dashboard asks for that key and stores it in browser local storage.

## Core behavior

Research runs use the following durable stages:

`WAITING -> SEARCHING -> FETCHING -> CHUNKING -> AI_ANALYSIS -> KNOWLEDGE_UPDATE -> COMPLETED`

A failure changes the run to `FAILED` while preserving evidence, chunks, AI chunk caches, and database state. Retrying a failed run first reconstructs candidates from persisted `RunEvidence` instead of depending on a fresh search result.

The knowledge layer contains Source, Chunk, Claim, Entity, Relation, Version, Conflict, RunEvidence, ClaimEvidence, ManualNote, and WebsiteWatch records. Raw HTML/PDF/text evidence is archived under `data/source`; structured chunk analysis is cached under `data/chunk`; knowledge snapshots are written under `data/knowledge`; version files are written under `data/history`; conflict snapshots are written under `data/conflict`.

Human priority policy:

- 100: human confirmed / manual evidence
- 80: human modified
- 50: AI-supported fact
- 20: AI inference

AI updates do not silently overwrite a higher-priority human value. Conflicts remain explicit until resolved.

## Search and evidence

The built-in search chain is:

1. Optional SearXNG JSON endpoint if `SEARXNG_URL` is configured.
2. DuckDuckGo HTML results.
3. Bing News RSS fallback.

No external search API key is required for the default profile, but public search providers can rate-limit or block automated traffic. For a controlled long-running deployment, set `SEARXNG_URL` to a SearXNG instance you operate.

Fetch safety includes private/local-address blocking by default, validation of each HTTP redirect before following it, a 25 MB document cap, unsupported binary MIME rejection, and prompt-injection instructions that treat fetched evidence as untrusted data. Set `ALLOW_PRIVATE_URLS=true` only if you intentionally need to monitor intranet URLs.

PDFs with little or no extractable text are still archived as raw evidence, but they do not produce useful chunks until text is available. v1.0 intentionally does not add a second OCR or embedding model.

## Single-model rule and vector directory

`data/vector` is reserved for forward compatibility. v1.0 does not introduce an embedding model because the architecture is locked to a single Qwen3.8-27B AI model. Retrieval in v1.0 uses topic-scoped persisted sources/chunks, lexical chunk relevance, claims, entities, relations, and evidence links.

## Scheduling

Default schedule:

- Daily research: 03:00, `Asia/Shanghai`.
- Website change checks: every 60 minutes.
- Celery worker concurrency: 1.

All values can be adjusted in `.env`, but the included defaults are the tested static production profile for the specified 40 GB / GTX 1650 host.

## Operations

```sh
sh doctor.sh
sh backup.sh
sh restore.sh /path/to/internetboard-YYYYMMDD-HHMMSS.tgz
```

`doctor.sh` reports container state, host/container GPU visibility, Ollama model state, processor split, health, and recent logs. `backup.sh` exports PostgreSQL plus the durable evidence/knowledge tree and configuration. `restore.sh` stops application services, restores data and PostgreSQL, and retains the previous data directory as `data.restore`.

## Security notes

Only the Nginx frontend port is published. PostgreSQL, Redis, backend API, and Ollama remain on the private Compose network. Protected `/api/*` routes require `X-API-Key`. Docker logs are rotated (`20m x 3` per service). The generated `.env` contains secrets and must not be published.

For Internet exposure, place the dashboard behind a QNAP reverse proxy with HTTPS and additional access controls. The default package is intended for trusted LAN/VPN access.
