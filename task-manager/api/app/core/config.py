# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # ✅ Синтаксис для Pydantic V2
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=".env",           # Ищет в рабочей директории (/app)
        env_file_encoding="utf-8",
        extra="ignore"
        )

    APP_NAME: str = "Task Manager"
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/tasks.db"
    REDIS_URL: str = "redis://redis:6379/2"

    # 🔑 Обязательные поля (без них сервис не запустится)
    WIDGET_SECRET: str
    JWT_SECRET_KEY: str
    NEXTCLOUD_APP_PASSWORD: str

    # Опциональные / со значениями по умолчанию
    MATRIX_HOMESERVER: str = "https://matrix.vpk-oil.ru"
    MATRIX_BOT_TOKEN: Optional[str] = None
    MATRIX_LOG_ROOM_ID: Optional[str] = None
    NEXTCLOUD_URL: str = "https://cloud.vpk-oil.ru"
    NEXTCLOUD_USER: str = "task_sync"
    NEXTCLOUD_CALENDAR_ID: str = "tasks"

# Инициализация вынесена в try/except для понятных ошибок
try:
    settings = Settings()
except Exception as e:
    print(f"🔴 CRITICAL: Failed to load settings. Check .env file!")
    print(f"   Details: {e}")
    raise