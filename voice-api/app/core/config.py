from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    # API
    VOICE_API_SECRET: str
    VOICE_API_PORT: int = 8000

    # Redis
    REDIS_URL: str = "redis://redis:6379/3"

    # Matrix
    MATRIX_HOMESERVER: str
    MATRIX_BOT_TOKEN: str
    MATRIX_FEEDBACK_ROOM_ID: str

    # STT
    WHISPER_MODEL: str = "small"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_LANGUAGE: str = "ru"
    WHISPER_PROMPT: str = ""

    # Storage
    VOICE_DATA_DIR: str = "/app/data/voice"
    VOICE_RETENTION_HOURS: int = 24

    # Asterisk
    VOICE_MAX_CONCURRENT: int = 2
    ASTERISK_CALLBACK_TIMEOUT: int = 90

settings = Settings()