# InternetBoard v1.0 Architecture

## Service graph

```text
Browser
  |
Nginx frontend :8788
  |
FastAPI backend
  |---------------- PostgreSQL 16 (knowledge + run state)
  |---------------- Redis 7.4 (Celery broker/result backend)
  |---------------- Ollama 0.32.13 (Qwen3.8-27B only)
  |
Celery worker (single concurrency)
Celery beat scheduler

One-shot model-init -> pulls exact configured Ollama model
```

## Research pipeline

```text
Topic/query
  -> search providers + website watches + manual evidence
  -> safe fetch + raw evidence archive
  -> content hash dedupe
  -> chunking + lexical relevance selection
  -> schema-constrained Qwen3.8 chunk analysis
  -> Claim / Entity / Relation persistence with evidence links
  -> research-gap follow-up search
  -> schema-constrained final synthesis
  -> Conflict persistence
  -> knowledge snapshot
```

## Reliability design

- One active run is serialized per Topic row when manual/scheduled work is queued.
- Worker concurrency and Ollama parallelism are both one.
- Celery late acknowledgements and worker-lost rejection protect in-flight work.
- Redis AOF is enabled and uses `noeviction` so broker keys are never silently discarded.
- Source content is deduplicated per topic; RunEvidence preserves each run/source relationship.
- Failed runs retain state; a retry reloads its persisted evidence before performing new network work.
- Chunk analysis cache keys include content hash, topic, query, model and prompt-cache version so unrelated topics cannot reuse stale analysis.
- JSON Schema is sent to Ollama and Pydantic validates every structured response; invalid output is regenerated up to three times.
- Human priorities dominate AI priorities and conflicts are explicit.
- Docker logs are bounded to avoid long-running NAS system-volume growth.

## Resource profile

The included profile deliberately caps context at 8192 and uses a single loaded model / single request. The Qwen3.8 Q4_K_M model is substantially larger than the GTX 1650 VRAM, so Ollama is expected to keep most model state in system RAM and offload only the portion that fits on the NVIDIA GPU. No fixed layer count is configured.

## Persistence map

Host root: `/share/Container/internetboard`

```text
postgres/       PostgreSQL cluster
redis/          Redis AOF data
ollama/         Ollama model files
data/source/    raw HTML/PDF/text evidence
data/chunk/     chunk-analysis JSON cache
data/knowledge/ knowledge snapshots
data/history/   version snapshots
data/conflict/  conflict snapshots
data/vector/    reserved, inactive in v1.0
backups/        backup archives
```
