import os
from dotenv import load_dotenv
load_dotenv()

HOMESERVER = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
USERNAME = os.getenv("MATRIX_BOT_USERNAME")
PASSWORD = os.getenv("MATRIX_BOT_PASSWORD")
MATRIX_DOMAIN = os.getenv("MATRIX_DOMAIN", "matrix.domain.local")

USE_TOKEN_AUTH=os.getenv("USE_TOKEN_AUTH", "false").lower() == "true"
BOT_ACCESS_TOKEN=os.getenv("BOT_ACCESS_TOKEN", None)



        
TIMEZONE = os.getenv("TIMEZONE", "UTC")
BOT_TOKEN = os.getenv("BOT_TOKEN", "...")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))