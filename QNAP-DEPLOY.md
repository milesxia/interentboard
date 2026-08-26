# InternetBoard v1.0 Production - QNAP TS-673A

## Runtime

- QNAP path: `/share/Container/internetboard`
- Web: `http://NAS_IP:8733`
- AI: `qwen3.8:27b-q4_K_M` only
- Ollama: `ollama/ollama:latest`
- InternetBoard images: `milesxia/internetboard-*:latest`
- NAS never builds application source.

## First start

On the first start only, if the database has no topics and the durable bootstrap marker does not exist, the built-in `config/topics.yml` definitions are inserted into PostgreSQL. After that, `/data/.bootstrap/topics-v1.json` prevents any image upgrade from re-applying or overwriting defaults. User edits remain authoritative.

## Handoff export

The web UI contains an `Export Handoff` action. It generates an LLM-oriented Markdown snapshot under `/share/Container/internetboard/data/exports` and downloads the same file to the browser. It contains topic/query definitions, manual notes, claims, recent research summaries, conflicts, graph relations and evidence excerpts.

## Upgrade

```bash
cd /share/Container/internetboard
docker compose pull
docker compose up -d
```

Persistent directories under `/share/Container/internetboard` are not removed during an image upgrade.

## Access

There is no InternetBoard application API-key prompt in the trusted-LAN profile. Do not expose the service directly to the public Internet; use HTTPS and access control at the reverse proxy/VPN layer if remote access is required.

## Visual evidence

InternetBoard automatically inspects useful images embedded in fetched HTML and image-heavy/scanned PDF pages with the same Qwen3.8 27B model. Visual evidence is bounded (default: max 2 assets per source and 4 per run), normalized before inference, deduplicated by image hash inside a topic, archived under `/share/Container/internetboard/data/visual`, linked to Claims/Entities/Relations, and included in the LLM handoff export. Logos, tiny icons and obvious QR/avatar assets are skipped heuristically.
