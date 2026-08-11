import json
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Загрузка глобальных шаблонов
TEMPLATES_FILE = "config/templates.json"
try:
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        TEMPLATES = json.load(f)
    logger.info(f"✅ Loaded {len(TEMPLATES)} global templates")
except Exception as e:
    logger.error(f"❌ Failed to load templates: {e}")
    TEMPLATES = {}


def _subscriber_key(raw: str) -> str:
    """
    Возвращает ключ уникальности абонента.

    Примеры:
    "89234743710:ACX:BCX" -> "89234743710"
    "4299:IT:Zavod"       -> "4299"
    """
    first_part = str(raw).split(":", 1)[0].strip()
    digits = "".join(ch for ch in first_part if ch.isdigit())
    return digits or str(raw).strip()

# Импортируем LOCATIONS из security, чтобы не дублировать чтение
from app.security import LOCATIONS

def get_template_by_id(template_id: str) -> dict | None:
    """Получает глобальный шаблон по ID"""
    tpl = TEMPLATES.get(template_id)
    if tpl:
        return {"id": template_id, **tpl}
    return None

def get_location_alerts(loc_id: str) -> list:
    """
    Возвращает настроенные алерты для локации, 
    обогащённые данными из глобальных шаблонов.
    """
    loc = LOCATIONS.get(loc_id, {})
    alerts = []
    for alert in loc.get("alerts", []):
        tpl_id = alert["template_id"]
        global_tpl = TEMPLATES.get(tpl_id, {})
        
        # Объединяем глобальные данные с локальными переопределениями
        merged_alert = {
            "template_id": tpl_id,
            "name": global_tpl.get("name", tpl_id),
            "severity": global_tpl.get("severity", "attention"),
            "audio_file": global_tpl.get("audio_file", "default.wav"),
            "text_for_matrix": alert.get("matrix_text", global_tpl.get("default_text", "")),
            "trigger_code": alert.get("trigger_code", ""),
            "subscribers": alert.get("subscribers", [])
        }
        alerts.append(merged_alert)
    
    return alerts