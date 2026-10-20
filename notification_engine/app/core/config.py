"""
config.py — Centralised, environment-driven settings via Pydantic v2 BaseSettings.

All tunables live here so nothing is hard-coded anywhere in the application.
"""


from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ---- Database --------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5434/notifications"
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---- Application -----------------------------------------------------
    app_title: str = "Distributed Notification Engine"
    app_version: str = "1.0.0"
    debug: bool = False

    # ---- Retry / backoff -------------------------------------------------
    retry_max_attempts: int = 5
    retry_base_delay: float = 0.5    # seconds
    retry_max_delay: float = 30.0    # seconds
    retry_jitter: float = 0.25       # ±fraction of computed delay

    # ---- Adapter hard timeouts -------------------------------------------
    # asyncio.wait_for() ceiling per adapter call; prevents stale workers.
    adapter_timeout_seconds: float = 10.0

    # ---- Rate limiting ---------------------------------------------------
    # Sliding-window cap: max requests per window (enforced in middleware).
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    # ---- Bulk write batching ---------------------------------------------
    # Max rows flushed in a single executemany / COPY cycle.
    bulk_batch_size: int = 500


settings = Settings()