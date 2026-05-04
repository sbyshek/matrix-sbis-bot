import os
import json
import time
import logging

logger = logging.getLogger(__name__)
TOKEN_FILE = "/app/data/sync_token.json"

def load_token() -> str | None:
    if os.path.exists(TOKEN_FILE):
        try:
            return json.load(open(TOKEN_FILE, "r")).get("token")
        except Exception as e:
            logger.warning(f"Failed to load sync token: {e}")
    return None

def save_token(token: str):
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({"token": token, "saved_at": time.time()}, f)
    except Exception as e:
        logger.warning(f"Failed to save sync token: {e}")