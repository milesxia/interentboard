# InternetBoard v1.0.0 Production

Architecture status: locked.

This release implements the first deployable production baseline for the TS-673A / 40 GB RAM / GTX 1650 profile.

Highlights:

- Single-model Qwen3.8-27B architecture.
- QNAP-oriented Docker Compose stack with PostgreSQL, Redis, Ollama, backend, worker, scheduler and frontend.
- Automatic exact-model initialization.
- Automatic NVIDIA visibility and post-inference GPU-participation checks.
- Raw evidence archive and traceable run/claim evidence links.
- Chunked AI processing with per-source and per-run budgets.
- Schema-constrained JSON output with repair retries.
- Claims, entities, relations, versions and explicit conflicts.
- Human-confirmed and human-edited priority policy.
- Manual input and dashboard-level claim confirmation/editing and conflict resolution.
- Multi-round gap-driven research.
- Website change detection.
- Daily 03:00 scheduling.
- Durable retry/resume from persisted RunEvidence and chunk AI caches.
- Backup, restore and diagnostic scripts.
- Redirect/private-network fetch protection and prompt-injection hardening.
- Bounded Docker logging and Redis broker no-eviction policy.

Runtime pin note: Ollama 0.32.13 is intentionally selected instead of 0.32.14. Qwen3.8-27B support landed in 0.32.12 and Qwen3.8 developer-instruction handling in 0.32.13. An open Linux/NVIDIA report against 0.32.14 describes unexpectedly high CPU consumption; avoiding that release is preferable on the CPU-constrained TS-673A profile.
