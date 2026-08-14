import json
import logging
from typing import Optional, Any

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None


async def init_redis() -> aioredis.Redis:
    """
    Инициализация Redis client.
    Вызывается один раз при старте приложения.
    """
    global _redis

    _redis = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True
    )

    await _redis.ping()
    logger.info("✅ Redis connected")

    return _redis


def get_redis() -> aioredis.Redis:
    """
    Возвращает текущий Redis client.
    """
    if _redis is None:
        raise RuntimeError("Redis is not initialized")

    return _redis


# =============================================================================
# Location state
# =============================================================================

def state_key(loc_id: str) -> str:
    return f"dispatch:state:{loc_id}"


def ttl_key(loc_id: str, event_id: str) -> str:
    return f"dispatch:ttl:{loc_id}:{event_id}"


def history_key(loc_id: str) -> str:
    return f"dispatch:history:{loc_id}"


async def init_locations_state(loc_ids: list[str]) -> None:
    """
    Создаёт пустое состояние для локаций, если его ещё нет.
    """
    redis = get_redis()

    for loc_id in loc_ids:
        key = state_key(loc_id)

        if not await redis.exists(key):
            await redis.set(
                key,
                json.dumps(
                    {
                        "events": [],
                        "updated_at": ""
                    },
                    ensure_ascii=False
                )
            )


async def get_state(loc_id: str) -> dict:
    """
    Возвращает текущее состояние локации.
    """
    redis = get_redis()

    raw = await redis.get(state_key(loc_id))

    if not raw:
        return {
            "events": [],
            "updated_at": ""
        }

    return json.loads(raw)


async def set_state(loc_id: str, state: dict) -> None:
    """
    Сохраняет состояние локации.
    """
    redis = get_redis()

    await redis.set(
        state_key(loc_id),
        json.dumps(state, ensure_ascii=False)
    )


async def set_event_ttl(loc_id: str, event_id: str, ttl_minutes: int) -> None:
    """
    Ставит TTL для автозакрытия события.
    """
    redis = get_redis()

    await redis.set(
        ttl_key(loc_id, event_id),
        "1",
        ex=ttl_minutes * 60
    )


async def delete_event_ttl(loc_id: str, event_id: str) -> None:
    """
    Удаляет TTL для события.
    """
    redis = get_redis()

    await redis.delete(ttl_key(loc_id, event_id))


async def get_ttl_keys() -> list[str]:
    """
    Возвращает все TTL-ключи.
    """
    redis = get_redis()

    return await redis.keys("dispatch:ttl:*")


async def key_exists(key: str) -> bool:
    """
    Проверка существования ключа.
    """
    redis = get_redis()

    return bool(await redis.exists(key))


# =============================================================================
# Старая история событий локации
# =============================================================================

async def get_event_history(loc_id: str) -> list[dict]:
    """
    Возвращает старую историю событий по локации.
    """
    redis = get_redis()

    raw = await redis.get(history_key(loc_id))

    if not raw:
        return []

    return json.loads(raw)


async def save_event_history(
    loc_id: str,
    event: dict,
    close_reason: str
) -> None:
    """
    Старая функция истории событий.

    Сохраняет закрытое событие в:
        dispatch:history:{loc_id}
    """
    redis = get_redis()

    event = dict(event)
    event["closed_at"] = event.get("closed_at") or ""
    event["close_reason"] = close_reason

    history = await get_event_history(loc_id)

    history.insert(0, event)
    history = history[:100]

    ttl_seconds = settings.HISTORY_TTL_HOURS * 3600

    await redis.set(
        history_key(loc_id),
        json.dumps(history, ensure_ascii=False),
        ex=ttl_seconds
    )

async def save_campaign_history(
    campaign_id: str,
    event: dict,
) -> None:
    """
    Сохраняет закрытую компанию:
        dispatch:campaign_launch:{campaign_id}
    """
    redis = get_redis()

    event = dict(event)
    ttl_seconds = settings.HISTORY_TTL_HOURS * 3600

    await redis.set(
        f"dispatch:campaign_launch:{campaign_id}",
        json.dumps(event, ensure_ascii=False),
        ex=ttl_seconds
    )
    
async def get_campaign_history(campaign_id: str) -> list[dict]:
    """
    Возвращает старую историю событий по локации.
    """
    redis = get_redis()

    raw = await redis.get(f"dispatch:campaign_launch:{campaign_id}")

    if not raw:
        return []

    return json.loads(raw)