import json
from pathlib import Path
import logging



logger = logging.getLogger(__name__)



# Загрузка шаблонов оповещений
ALERT_TEMPLATES_FILE = "config/alert_templates.json"
try:
    with open(ALERT_TEMPLATES_FILE, "r", encoding="utf-8") as f:
        ALERT_CONFIG = json.load(f)
    logger.info(f"✅ Loaded {len(ALERT_CONFIG.get('audio_templates', []))} alert templates")
except Exception as e:
    logger.error(f"❌ Failed to load alert templates: {e}")
    ALERT_CONFIG = {"audio_templates": [], "locations": {}}

def get_template_by_id(template_id: str) -> dict | None:
    for t in ALERT_CONFIG.get("audio_templates", []):
        if t["id"] == template_id:
            return t
    return None

def resolve_subscribers(loc_config: dict, event_config: dict) -> list[str]:
    """Возвращает список абонентов с учетом наследования"""
    event_subs = event_config.get("subscribers")
    if event_subs is not None and len(event_subs) > 0:
        return event_subs
    return loc_config.get("default_subscribers", [])

