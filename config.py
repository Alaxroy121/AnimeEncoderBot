"""
Configuration loader for AnimeEncoderBot.
Reads settings from config.env and environment variables.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if it exists
env_path = Path(__file__).parent / "config.env"
if env_path.exists():
    load_dotenv(env_path)


class Config:
    """Bot configuration loaded from environment variables."""

    # Telegram credentials
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")

    # Admin user IDs
    ADMIN_IDS: list[int] = [
        int(uid.strip())
        for uid in os.getenv("ADMIN_IDS", "").split(",")
        if uid.strip().isdigit()
    ]

    # MongoDB
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017/anime_encoder_bot")

    # Logging channel
    LOG_CHANNEL: int = int(os.getenv("LOG_CHANNEL", "0"))

    # File handling
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "./downloads")
    MAX_FILE_SIZE: int = int(os.getenv("MAX_FILE_SIZE", str(2 * 1024 * 1024 * 1024)))  # 2GB

    # Encoding defaults
    DEFAULT_CODEC: str = os.getenv("DEFAULT_CODEC", "hevc").lower()
    GPU_ENABLED: bool = os.getenv("GPU_ENABLED", "true").lower() == "true"

    # Queue
    CONCURRENT_TASKS: int = int(os.getenv("CONCURRENT_TASKS", "2"))

    # Telegram upload limit (2GB minus safety margin)
    TG_UPLOAD_LIMIT: int = 2 * 1024 * 1024 * 1024 - 1024 * 1024  # ~2GB - 1MB margin

    # Google Drive (for files > 2GB)
    # OAuth2 credentials (preferred — works with personal Gmail)
    GDRIVE_CLIENT_ID: str = os.getenv("GDRIVE_CLIENT_ID", "")
    GDRIVE_CLIENT_SECRET: str = os.getenv("GDRIVE_CLIENT_SECRET", "")
    GDRIVE_REFRESH_TOKEN: str = os.getenv("GDRIVE_REFRESH_TOKEN", "")
    # Legacy Service Account (deprecated — Google removed SA storage quota)
    GDRIVE_SA_JSON: str = os.getenv("GDRIVE_SA_JSON", "./sa.json")
    GDRIVE_FOLDER_ID: str = os.getenv("GDRIVE_FOLDER_ID", "")

    # Progress update throttle (seconds)
    PROGRESS_UPDATE_INTERVAL: float = 5.0

    # Task timeout (seconds) — 6 hours default
    TASK_TIMEOUT: int = int(os.getenv("TASK_TIMEOUT", "21600"))

    # Real-CUGAN binary path and performance tuning
    REALESRGAN_PATH: str = os.getenv("REALESRGAN_PATH", "realesrgan-ncnn-vulkan")
    REALESRGAN_GPU_IDS: str = os.getenv("REALESRGAN_GPU_IDS", "auto")  # auto, 0, or 0,1
    REALESRGAN_THREADS: str = os.getenv("REALESRGAN_THREADS", "2:4:2")  # load:process:save
    REALESRGAN_TILE_SIZE: int = int(os.getenv("REALESRGAN_TILE_SIZE", "0"))  # 0 = auto
    REALESRGAN_OUTPUT_FORMAT: str = os.getenv("REALESRGAN_OUTPUT_FORMAT", "jpg").lower()
    UPSCALE_SEGMENT_SECONDS: int = int(os.getenv("UPSCALE_SEGMENT_SECONDS", "10"))
    UPSCALE_PARALLEL_JOBS: int = int(os.getenv("UPSCALE_PARALLEL_JOBS", "0"))  # 0 = one job per GPU
    UPSCALE_JOBS_PER_GPU: int = int(os.getenv("UPSCALE_JOBS_PER_GPU", "1"))

    # Supported video extensions
    SUPPORTED_EXTENSIONS: set[str] = {
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
        ".wmv", ".m4v", ".ts", ".m2ts", ".vob",
    }

    @classmethod
    def validate(cls) -> list[str]:
        """Validate required config values. Returns list of error messages."""
        errors = []
        if not cls.BOT_TOKEN or cls.BOT_TOKEN == "your_bot_token_here":
            errors.append("BOT_TOKEN is not set")
        if not cls.API_ID:
            errors.append("API_ID is not set")
        if not cls.API_HASH or cls.API_HASH == "your_api_hash_here":
            errors.append("API_HASH is not set")
        if not cls.ADMIN_IDS:
            errors.append("ADMIN_IDS is not set")
        return errors

    @classmethod
    def ensure_dirs(cls) -> None:
        """Create required directories."""
        Path(cls.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
