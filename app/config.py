import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


class Settings:
    """Runtime configuration, read from environment variables."""

    def __init__(self) -> None:
        self.upstream_base_url: str = os.getenv(
            "UPSTREAM_BASE_URL", "https://hospital-directory.onrender.com"
        ).rstrip("/")
        self.data_dir: str = os.getenv("DATA_DIR", "data")
        self.max_rows: int = _int("MAX_ROWS", 20)  # brief: max 20 hospitals; 0 = no limit
        self.max_file_bytes: int = _int("MAX_FILE_BYTES", 1_000_000)
        self.upstream_timeout: float = _float("UPSTREAM_TIMEOUT_SECONDS", 30.0)
        self.upstream_retries: int = _int("UPSTREAM_RETRIES", 2)
        # Before sending creates, wait up to this long for upstream to answer GET /
        # (a sleeping free-tier instance takes ~30-60 s to wake). 0 = skip the check.
        self.upstream_warmup: float = _float("UPSTREAM_WARMUP_SECONDS", 60.0)
        self.retry_backoff: float = _float("UPSTREAM_RETRY_BACKOFF_SECONDS", 0.5)
        # Max upstream creates in flight across all batches in this process (1 = sequential).
        self.upstream_concurrency: int = max(1, _int("UPSTREAM_CONCURRENCY", 10))
        # Stop a batch (status `interrupted`, resumable) after this many consecutive
        # transient upstream failures. 0 disables the check.
        self.outage_threshold: int = _int("UPSTREAM_OUTAGE_THRESHOLD", 5)
        # The destructive "delete every batch" endpoint. Off unless explicitly enabled
        # (secure by default): a missing or misspelt value keeps it off.
        self.enable_hard_clean: bool = os.getenv("ENABLE_HARD_CLEAN", "false").strip().lower() in (
            "1", "true", "yes", "on"
        )


def get_settings() -> Settings:
    return Settings()
