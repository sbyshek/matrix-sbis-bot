import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

UNIFIED_HISTORY_GLOBAL_KEY = "dispatch:unified_history"
UNIFIED_HISTORY_LOC_KEY_TEMPLATE = "dispatch:unified_history:{loc_id}"
UNIFIED_HISTORY_LIMIT = 1000


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Novosibirsk")


async def save_unified_history(
    *,
    loc_id: str,
    loc_name: str,
    category: str,
    action: str,
    severity: Optional[str] = None,
    title: str = "",
    description: str = "",
    source: str = "",
    event_id: Optional[str] = None,
    template_id: Optional[str] = None,
    template_name: Optional[str] = None,
    campaign_id: Optional[str] = None,
    subscribers_count: Optional[int] = None,
    close_reason: Optional[str] = None
) -> None:
    """
    Единая история дашборда.

    category:
        incident | campaign | system

    action:
        created | closed | cleared | auto_closed | normal | triggered | failed
    """
    now_utc = datetime.now(timezone.utc)
    local_tz = _local_tz()

    record = {
        "id": uuid.uuid4().hex[:12],
        "ts_utc": now_utc.isoformat(),
        "ts_local": now_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M:%S"),
        "timezone": str(local_tz),

        "loc_id": loc_id,
        "loc_name": loc_name,

        "category": category,
        "action": action,

        "severity": severity,
        "title": title,
        "description": description,

        "source": source,

        "event_id": event_id,
        "template_id": template_id,
        "template_name": template_name,
        "campaign_id": campaign_id,
        "subscribers_count": subscribers_count,

        "close_reason": close_reason
    }

    try:
        redis = get_redis()

        global_key = UNIFIED_HISTORY_GLOBAL_KEY
        loc_key = UNIFIED_HISTORY_LOC_KEY_TEMPLATE.format(loc_id=loc_id)

        payload = json.dumps(record, ensure_ascii=False)

        pipe = redis.pipeline()

        pipe.lpush(global_key, payload)
        pipe.ltrim(global_key, 0, UNIFIED_HISTORY_LIMIT - 1)

        pipe.lpush(loc_key, payload)
        pipe.ltrim(loc_key, 0, UNIFIED_HISTORY_LIMIT - 1)

        await pipe.execute()

        logger.info(
            f"✅ Unified history saved: {category}/{action} | "
            f"{loc_id} | {title}"
        )

    except Exception as e:
        logger.error(f"❌ Failed to save unified history: {e}")


async def get_unified_history(
    *,
    limit: int = 100,
    loc_id: Optional[str] = None,
    category: Optional[str] = None,
    action: Optional[str] = None,
    template_id: Optional[str] = None
) -> list[dict]:
    """
    Возвращает единую историю из глобального списка.
    """
    redis = get_redis()

    limit = max(1, min(limit, 500))

    raw_items = await redis.lrange(
        UNIFIED_HISTORY_GLOBAL_KEY,
        0,
        UNIFIED_HISTORY_LIMIT - 1
    )

    result = []

    for raw in raw_items:
        try:
            item = json.loads(raw)
        except Exception:
            continue

        if loc_id and item.get("loc_id") != loc_id:
            continue

        if category and item.get("category") != category:
            continue

        if action and item.get("action") != action:
            continue

        if template_id and item.get("template_id") != template_id:
            continue

        result.append(item)

        if len(result) >= limit:
            break

    return result


async def get_location_unified_history(
    loc_id: str,
    *,
    limit: int = 100,
    category: Optional[str] = None,
    action: Optional[str] = None,
    template_id: Optional[str] = None
) -> list[dict]:
    """
    Возвращает единую историю конкретной локации.
    """
    redis = get_redis()

    limit = max(1, min(limit, 500))

    loc_key = UNIFIED_HISTORY_LOC_KEY_TEMPLATE.format(loc_id=loc_id)

    raw_items = await redis.lrange(
        loc_key,
        0,
        UNIFIED_HISTORY_LIMIT - 1
    )

    result = []

    for raw in raw_items:
        try:
            item = json.loads(raw)
        except Exception:
            continue

        if category and item.get("category") != category:
            continue

        if action and item.get("action") != action:
            continue

        if template_id and item.get("template_id") != template_id:
            continue

        result.append(item)

        if len(result) >= limit:
            break

    return result