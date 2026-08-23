from pathlib import Path
import json
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

PLAY_COUNTS_FILE = Path("config/play_counts.json")

def get_play_count(number: str) -> int:
    """Число повторов для номера: переопределение из json или дефолт из .env"""
    try:
        if PLAY_COUNTS_FILE.exists():
            data = json.loads(PLAY_COUNTS_FILE.read_text(encoding="utf-8"))
            if str(number) in data:
                return int(data[str(number)])
    except Exception as e:
        logger.warning(f"⚠️ play_counts.json read error: {e}")
    return settings.DEFAULT_PLAY_COUNT
