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
    
    # 🔥 Нормализация абонентов: резолвим из мастер-списка локации
    raw_alerts = LOCATIONS.get(loc_id, {}).get("alerts", [])
    for a, raw in zip(alerts, raw_alerts):
        a["subscribers"] = resolve_subscribers(loc_id, raw)
    
    return alerts


def get_location_master_subscribers(loc_id: str) -> list:
    """Мастер-список абонентов локации (новая схема)."""
    return LOCATIONS.get(loc_id, {}).get("subscribers", []) or []


def resolve_subscribers(loc_id: str, raw_alert: dict) -> list:
    """
    Новая схема: location.subscribers — источник, alert.subscribers — фильтр (подмножество).
    Старая схема (мастера нет): fallback на alert.subscribers.
    """
    master = get_location_master_subscribers(loc_id)
    own = raw_alert.get("subscribers") or []

    if not master:                      # конфиг ещё не мигрирован
        return own

    if not own:                         # фильтра нет → вся локация
        return master

    keys = {str(s).split(":", 1)[0].strip() for s in own}
    subset = [s for s in master if str(s).split(":", 1)[0].strip() in keys]

    # мягкость: номера из фильтра, отсутствующие в мастере, не теряем
    have = {str(s).split(":", 1)[0].strip() for s in subset}
    subset += [s for s in own if str(s).split(":", 1)[0].strip() not in have]
    return subset


def get_all_location_subscribers(loc_id: str) -> list:
    """
    Все уникальные абоненты локации:
    мастер-список (новая схема) или union по сценариям (старая схема).
    """
    master = get_location_master_subscribers(loc_id)
    if master:
        return master

    seen = {}
    for a in get_location_alerts(loc_id):
        for s in a.get("subscribers", []):
            seen.setdefault(str(s).split(":", 1)[0].strip(), s)
    return list(seen.values())