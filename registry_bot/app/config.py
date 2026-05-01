import os
from dotenv import load_dotenv
load_dotenv()

# Matrix
HOMESERVER = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
USERNAME = os.getenv("MATRIX_BOT_USERNAME")
PASSWORD = os.getenv("MATRIX_BOT_PASSWORD")
MATRIX_DOMAIN = os.getenv("MATRIX_DOMAIN", "matrix.domain.local")

# SMB / Windows Share
SMB_SERVER = os.getenv("SMB_SERVER")
SMB_SHARE = os.getenv("SMB_SHARE")
SMB_DOMAIN = os.getenv("SMB_DOMAIN", "ST-OIL")
SMB_USERNAME = os.getenv("SMB_USERNAME")
SMB_PASSWORD = os.getenv("SMB_PASSWORD")

# Маппинг путей
CLIENT_DRIVE = os.getenv("CLIENT_DRIVE", "N:").upper().rstrip("\\/")
# pysmb работает с прямыми слешами
SMB_BASE = f"//{SMB_SERVER}/{SMB_SHARE}"

# System
TIMEZONE = os.getenv("TIMEZONE", "UTC+7")