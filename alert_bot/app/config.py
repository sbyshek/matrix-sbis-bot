import os
from dotenv import load_dotenv
load_dotenv()

HOMESERVER = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
USERNAME = os.getenv("MATRIX_BOT_USERNAME")
PASSWORD = os.getenv("MATRIX_BOT_PASSWORD")
MATRIX_DOMAIN = os.getenv("MATRIX_DOMAIN", "matrix.domain.local")

# Парсинг маршрутов: room1=id1,room2=id2
# _raw_routes = os.getenv("ALERT_ROOMS", "")
# ALERT_ROOMS = {}
# for pair in _raw_routes.split(","):
#     if ":" in pair:
#         k, v = pair.strip().split(":", 1)
#         ALERT_ROOMS[k.strip()] = v.strip()
        
        
TIMEZONE = os.getenv("TIMEZONE", "UTC")
BOT_TOKEN = os.getenv("BOT_TOKEN", "...")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))