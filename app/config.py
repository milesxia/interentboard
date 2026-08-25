from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psutil
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
    topics_file: Path = Path(os.getenv("TOPICS_FILE", str(BASE_DIR / "config" / "topics.yml")))
    baseline_file: Path = Path(os.getenv("BASELINE_FILE", str(BASE_DIR / "seed" / "baseline-v2.8.md")))
    timezone: str = os.getenv("TZ", "Asia/Shanghai")
    schedule_hour: int = _int("SCHEDULE_HOUR", 3)
    schedule_minute: int = _int("SCHEDULE_MINUTE", 0)
    admin_password: str = os.getenv("ADMIN_PASSWORD", "change-me-now")
    session_secret: str = os.getenv("SESSION_SECRET", "please-change-this-session-secret")
    session_https_only: bool = _bool("SESSION_HTTPS_ONLY", False)
    login_max_attempts: int = _int("LOGIN_MAX_ATTEMPTS", 8)
    login_window_seconds: int = _int("LOGIN_WINDOW_SECONDS", 600)

    # Ollama: small text-only extractor keeps the GTX 1650 busy; Qwen3.8 is reserved for final reasoning.
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3.8:27b-q4_K_M")
    ollama_extract_model: str = os.getenv("OLLAMA_EXTRACT_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
    auto_pull_model: bool = _bool("AUTO_PULL_MODEL", True)
    mock_ai: bool = _bool("MOCK_AI", False)
    ollama_num_ctx: int = _int("OLLAMA_NUM_CTX", 8192)
    ollama_num_gpu: int = _int("OLLAMA_NUM_GPU", 4)
    ollama_extract_num_gpu: int = _int("OLLAMA_EXTRACT_NUM_GPU", 99)
    ollama_num_thread: int = _int("OLLAMA_NUM_THREAD", 6)
    ollama_extract_num_thread: int = _int("OLLAMA_EXTRACT_NUM_THREAD", 4)
    ollama_final_think: str = os.getenv("OLLAMA_FINAL_THINK", "medium").strip().lower()
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE_REQUEST", "10m")

    # AI pipeline budgets. The app splits before Ollama, and sends truncate=false.
    ai_chunk_tokens: int = _int("AI_CHUNK_TOKENS", 2400)
    ai_chunk_overlap_tokens: int = _int("AI_CHUNK_OVERLAP_TOKENS", 180)
    ai_extract_predict: int = _int("AI_EXTRACT_PREDICT", 320)
    ai_reduce_tokens: int = _int("AI_REDUCE_TOKENS", 3200)
    ai_reduce_predict: int = _int("AI_REDUCE_PREDICT", 500)
    ai_final_tokens: int = _int("AI_FINAL_TOKENS", 5000)
    ai_final_predict: int = _int("AI_FINAL_PREDICT", 1200)
    max_history_claims: int = _int("MAX_HISTORY_CLAIMS", 60)
    backfill_evidence_per_run: int = _int("BACKFILL_EVIDENCE_PER_RUN", 3)

    # Long-term retrieval / agentic follow-up. Semantic RAG stays dormant on small KBs
    # so the NAS does not constantly swap a third model in and out of VRAM.
    embedding_model: str = os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b")
    enable_embeddings: bool = _bool("ENABLE_EMBEDDINGS", True)
    semantic_rag_min_claims: int = _int("SEMANTIC_RAG_MIN_CLAIMS", 200)
    embedding_batch_size: int = _int("EMBEDDING_BATCH_SIZE", 24)
    semantic_candidate_limit: int = _int("SEMANTIC_CANDIDATE_LIMIT", 80)
    enable_gap_search: bool = _bool("ENABLE_GAP_SEARCH", True)
    max_followup_queries: int = _int("MAX_FOLLOWUP_QUERIES", 2)
    queue_poll_seconds: int = _int("QUEUE_POLL_SECONDS", 2)
    queue_max_attempts: int = _int("QUEUE_MAX_ATTEMPTS", 3)
    auto_backup_keep: int = _int("AUTO_BACKUP_KEEP", 7)

    # Search / fetch
    max_search_results: int = _int("MAX_SEARCH_RESULTS", 8)
    max_candidates_per_topic: int = _int("MAX_CANDIDATES_PER_TOPIC", 20)
    max_fetch_concurrency: int = _int("MAX_FETCH_CONCURRENCY", 3)
    request_timeout: int = _int("REQUEST_TIMEOUT", 25)
    searxng_url: str = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    archive_fulltext: bool = _bool("ARCHIVE_FULLTEXT", True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "intelboard.db"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def manual_archive_dir(self) -> Path:
        return self.data_dir / "manual"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.manual_archive_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


def total_memory_gb() -> float:
    return psutil.virtual_memory().total / (1024 ** 3)


def choose_model(requested: str = "auto", memory_gb: float | None = None) -> str:
    if requested and requested != "auto":
        return requested
    gb = total_memory_gb() if memory_gb is None else memory_gb
    if gb >= 36:
        return "qwen3.8:27b-q4_K_M"
    if gb >= 20:
        return "qwen3.5:9b"
    if gb >= 12:
        return "qwen3.5:4b"
    return "qwen3.5:2b"


def load_topics(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    topics = doc.get("topics", [])
    if not isinstance(topics, list):
        raise ValueError("topics.yml: topics must be a list")
    return topics


settings = Settings()
