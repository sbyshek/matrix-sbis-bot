import os
import json
import logging

logger = logging.getLogger(__name__)
CURSOR_FILE = "/app/data/alert_cursor.json"

def load_cursor() -> str | None:
    if os.path.exists(CURSOR_FILE):
        try:
            with open(CURSOR_FILE, "r") as f:
                data = json.load(f)
                logger.info(f"📂 Cursor loaded: {data['timestamp'][:19]}")
                return data["timestamp"]
        except Exception as e:
            logger.warning(f"Failed to parse cursor: {e}")
    return None

def save_cursor(timestamp: str):
    try:
        os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
        with open(CURSOR_FILE, "w") as f:
            json.dump({"timestamp": timestamp}, f, indent=2)
        logger.debug(f"💾 Cursor saved: {timestamp[:19]}")
    except Exception as e:
        logger.error(f"❌ Failed to save cursor to {CURSOR_FILE}: {e}")