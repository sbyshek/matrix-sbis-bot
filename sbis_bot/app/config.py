import os
import re
from dotenv import load_dotenv
from datetime import timezone, timedelta

load_dotenv()

# === MATRIX ===
HOMESERVER = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
USERNAME = os.getenv("MATRIX_BOT_USERNAME")
PASSWORD = os.getenv("MATRIX_BOT_PASSWORD")
TARGET_ROOM = os.getenv("MATRIX_ROOM_ID", "").strip() or None
MATRIX_DOMAIN = os.getenv("MATRIX_DOMAIN", "matrix.vpk-oil.ru")
ADMIN_ROOM_ID = os.getenv("ADMIN_ROOM_ID")

# === LDAP (из старого бота) ===
LDAP_SERVER = os.getenv("LDAP_SERVER")
LDAP_USER = os.getenv("LDAP_USER")
LDAP_PASSWORD = os.getenv("LDAP_PASSWORD")
LDAP_SEARCHBASE = os.getenv("LDAP_SEARCHBASE")
ALLOWED_USERS_GROUP = os.getenv("ALLOWED_USERS_GROUP")

# === MIKROTIK (из старого бота) ===
ROUTEROS_HOST = os.getenv("ROUTEROS_HOST")
ROUTEROS_PORT = os.getenv("ROUTEROS_PORT", "22")
ROUTEROS_USERNAME = os.getenv("ROUTEROS_USERNAME")
ROUTEROS_PASSWORD = os.getenv("ROUTEROS_PASSWORD")
ROUTEROS_ADDRESSLIST = os.getenv("ROUTEROS_ADDRESSLIST", "VPN_ACCESS")
ROUTEROS_BLOCK_RULE_COMMENT = os.getenv("ROUTEROS_BLOCK_RULE_COMMENT")

# === ADMIN ===
_admin_raw = os.getenv("ADMIN_LOGINS", "").split(',')
ADMIN_LOGINS = [
    f"@{admin.strip().lower()}:{MATRIX_DOMAIN}"
    for admin in _admin_raw
    if admin.strip()
]

ADMIN_USERNAMES = {
    admin.strip().lower()
    for admin in _admin_raw
    if admin.strip()
}

# === SYSTEM / LOGGING ===
# Часовой пояс (парсинг вида UTC+7)
# === SYSTEM / LOGGING ===


TIMEZONE_STR = os.getenv("TIMEZONE", "UTC+7").strip()
try:
    tz_match = re.match(r'^(?:UTC)?([+-]?)(\d{1,2})(?::?(\d{2}))?$', TIMEZONE_STR, re.IGNORECASE)
    if tz_match:
        sign = -1 if tz_match.group(1) == '-' else 1
        hours = int(tz_match.group(2))
        mins = int(tz_match.group(3) or 0)
        TIMEZONE_TZINFO = timezone(sign * timedelta(hours=hours, minutes=mins))
    else:
        TIMEZONE_TZINFO = timezone.utc
except Exception:
    TIMEZONE_TZINFO = timezone.utc

# Пути для данных и логов (адаптируем под Docker /app)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
LOG_FILE = os.getenv("LOG_FILE", "/app/logs/bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")