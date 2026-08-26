from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "InternetBoard"
    app_version: str = "1.0.0"
    environment: str = "production"
    timezone: str = "Asia/Shanghai"
    data_dir: Path = Path("/data")

    database_url: str = "postgresql+psycopg://internetboard:internetboard@postgres:5432/internetboard"
    redis_url: str = "redis://redis:6379/0"

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3.8:27b-q4_K_M"
    ollama_context_length: int = 8192
    ollama_keep_alive: str = "10m"
    ollama_timeout_seconds: int = 900
    ollama_json_retries: int = 3
    ollama_use_mmap: bool = True
    ollama_use_mlock: bool = True
    ollama_pin_model: bool = True
    ollama_num_predict_chunk: int = 1200
    ollama_num_predict_synthesis: int = 1800

    search_timeout_seconds: int = 25
    fetch_timeout_seconds: int = 45
    max_search_rounds: int = 2
    max_queries_per_round: int = 6
    max_results_per_query: int = 8
    max_sources_per_run: int = 8
    max_total_ai_chunks: int = 12
    max_ai_chunks_per_source: int = 3
    visual_enabled: bool = True
    visual_max_assets_per_source: int = 2
    visual_max_assets_per_run: int = 4
    visual_max_candidates_per_source: int = 10
    visual_max_image_bytes: int = 8 * 1024 * 1024
    visual_min_width: int = 280
    visual_min_height: int = 160
    visual_min_pixels: int = 100_000
    visual_max_dimension: int = 1600
    visual_request_timeout_seconds: int = 30
    visual_pdf_max_pages: int = 2
    visual_pdf_text_threshold: int = 350
    visual_pdf_scale: float = 1.4
    visual_num_predict: int = 1000
    max_fetch_bytes: int = 25 * 1024 * 1024
    chunk_chars: int = 5500
    chunk_overlap_chars: int = 500
    min_chunk_chars: int = 300
    allow_private_urls: bool = False
    search_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/151.0 Safari/537.36 InternetBoard/1.0"
    )
    searxng_url: str = ""

    scheduler_hour: int = 3
    scheduler_minute: int = 0
    website_watch_minutes: int = 60
    max_run_retries: int = 2
    run_heartbeat_interval_seconds: int = 15
    run_heartbeat_ttl_seconds: int = 60
    run_lock_ttl_seconds: int = 90
    run_queue_marker_ttl_seconds: int = 180
    worker_heartbeat_interval_seconds: int = 15
    worker_heartbeat_ttl_seconds: int = 50
    runtime_watchdog_seconds: int = 60

    @property
    def source_dir(self) -> Path:
        return self.data_dir / "source"

    @property
    def chunk_dir(self) -> Path:
        return self.data_dir / "chunk"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def conflict_dir(self) -> Path:
        return self.data_dir / "conflict"
    @property
    def visual_dir(self) -> Path:
        return self.data_dir / "visual"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.source_dir,
            self.chunk_dir,
            self.knowledge_dir,
            self.history_dir,
            self.conflict_dir,
            self.visual_dir,
            self.data_dir / "vector",
            self.data_dir / "exports",
            self.data_dir / ".bootstrap",
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


settings = get_settings()
