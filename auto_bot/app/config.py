import os
from dotenv import load_dotenv

load_dotenv()

# === MATRIX ===
HOMESERVER = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
USERNAME = os.getenv("MATRIX_BOT_USERNAME")
PASSWORD = os.getenv("MATRIX_BOT_PASSWORD")
MATRIX_DOMAIN = os.getenv("MATRIX_DOMAIN", "matrix.domain.local")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

# === РОЛИ (Access Control) ===
# Админы и Транспортная служба видят ВЕСЬ транспорт
# Обычные пользователи видят только АВТОБУСЫ
_raw_admins = os.getenv("AUTO_ADMINS", "").split(',')
_raw_transport = os.getenv("AUTO_TRANSPORT", "").split(',')

# Формируем полные ID для проверки прав (@user:domain)
AUTO_ADMINS = {f"@{u.strip().lower()}:{MATRIX_DOMAIN}" for u in _raw_admins if u.strip()}
AUTO_TRANSPORT = {f"@{u.strip().lower()}:{MATRIX_DOMAIN}" for u in _raw_transport if u.strip()}
VISIBLE_ALL_ROLES = AUTO_ADMINS | AUTO_TRANSPORT

# === GLONASS SOFT ===
GLONASS_URL = os.getenv("GLONASS_URL", "https://hosting.glonasssoft.ru/api/v3/")
GLONASS_LOGIN = os.getenv("GLONASS_SOFT_LOGIN")
GLONASS_PASSWORD = os.getenv("GLONASS_SOFT_PASSWORD")

# Список ID транспортных средств
def parse_ids(env_key: str) -> list[int]:
    return [int(x.strip()) for x in os.getenv(env_key, "").split(',') if x.strip().isdigit()]

BUS_IDS = parse_ids("BUS_IDS")
TRUCK_IDS = parse_ids("TRUCK_IDS")
CAR_IDS = parse_ids("CAR_IDS")

# === SYSTEM / REDIS ===
# Redis используется для кеширования токена авторизации и координат
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL_TOKEN = int(os.getenv("CACHE_TTL_TOKEN", "1800")) # 30 мин
CACHE_TTL_DATA = int(os.getenv("CACHE_TTL_DATA", "60"))      # 1 мин
TIMEZONE = os.getenv("TIMEZONE", "UTC+7")


# config.py
import os

# ... существующий код ...

# 🔑 Настройки для виджетов
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://vpk-oil.ru")
WIDGET_ACCESS_TOKEN = os.getenv("WIDGET_ACCESS_TOKEN", "")
ADMINS = set(u.strip() for u in os.getenv("ADMINS", "").split(",") if u.strip())

# 🧩 Реестр виджетов (масштабируется без правки кода бота)
WIDGET_TYPES = {
    "bus": {
        "url_pattern": "/bus/{vehicle_id}",
        "name": "🚌 Карта автобуса",
        "description": "Живое местоположение автобуса",
        "required_args": ["vehicle_id"],
        "admin_only": True
    },
    "truck": {
        "url_pattern": "/bus/{vehicle_id}",
        "name": "🚛 Карта грузовика",
        "description": "Мониторинг грузового транспорта",
        "required_args": ["vehicle_id"],
        "admin_only": True
    },
    "queue": {
        "url_pattern": "/queue/widget",
        "name": "⏱ Электронная очередь",
        "description": "Статус очереди на погрузку",
        "required_args": [],
        "admin_only": False
    },
    # Пример будущего виджета:
    # "weather": {
    #     "url_pattern": "/weather/{city}",
    #     "name": "🌤 Погода",
    #     "description": "Прогноз погоды",
    #     "required_args": ["city"],
    #     "admin_only": False
    # }
}