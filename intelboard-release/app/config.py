from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import psutil
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
    topics_file: Path = Path(os.getenv("TOPICS_FILE", str(BASE_DIR / "config" / "topics.yml")))
    baseline_file: Path = Path(os.getenv("BASELINE_FILE", str(BASE_DIR / "seed" / "baseline-v2.8.md")))
    timezone: str = os.getenv("TZ", "Asia/Shanghai")
    schedule_hour: int = int(os.getenv("SCHEDULE_HOUR", "3"))
    schedule_minute: int = int(os.getenv("SCHEDULE_MINUTE", "0"))
    admin_password: str = os.getenv("ADMIN_PASSWORD", "change-me-now")
    session_secret: str = os.getenv("SESSION_SECRET", "please-change-this-session-secret")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "auto")
    auto_pull_model: bool = _bool("AUTO_PULL_MODEL", True)
    mock_ai: bool = _bool("MOCK_AI", False)
    max_search_results: int = int(os.getenv("MAX_SEARCH_RESULTS", "8"))
    max_candidates_per_topic: int = int(os.getenv("MAX_CANDIDATES_PER_TOPIC", "20"))
    max_fetch_concurrency: int = int(os.getenv("MAX_FETCH_CONCURRENCY", "3"))
    ai_context_chars: int = int(os.getenv("AI_CONTEXT_CHARS", "18000"))
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    request_timeout: int = int(os.getenv("REQUEST_TIMEOUT", "25"))
    searxng_url: str = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    archive_fulltext: bool = _bool("ARCHIVE_FULLTEXT", True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "intelboard.db"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


def total_memory_gb() -> float:
    return psutil.virtual_memory().total / (1024 ** 3)


def choose_model(requested: str = "auto", memory_gb: float | None = None) -> str:
    if requested and requested != "auto":
        return requested
    gb = total_memory_gb() if memory_gb is None else memory_gb
    # TS-673A + 40GB RAM profile: use the stronger MoE model for final synthesis.
    # Only one inference is allowed at a time, and context is capped to protect RAM.
    if gb >= 36:
        return "qwen3:30b-a3b-instruct-2507-q4_K_M"
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
