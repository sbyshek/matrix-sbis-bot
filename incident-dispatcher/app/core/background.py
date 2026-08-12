import asyncio
import json
import logging

from app.core.redis import (
    get_redis,
    get_ttl_keys,
    key_exists,
    get_state,
    set_state,
    save_event_history
)
from app.core.history import save_unified_history
from app.core.matrix import send_event_to_matrix
from app.security import LOCATIONS

logger = logging.getLogger(__name__)


async def auto_close_checker() -> None:
    """
    Фоновая задача автозакрытия событий.
    """
    while True:
        try:
            ttl_keys = await get_ttl_keys()

            for ttl_key in ttl_keys:
                exists = await key_exists(ttl_key)

                if exists:
                    continue

                parts = ttl_key.split(":")

                if len(parts) != 4:
                    continue

                loc_id = parts[2]
                event_id = parts[3]

                state = await get_state(loc_id)

                event_to_close = None

                for event in state.get("events", []):
                    if event.get("id") == event_id:
                        event_to_close = event
                        break

                if not event_to_close:
                    continue

                state["events"] = [
                    event for event in state["events"]
                    if event.get("id") != event_id
                ]

                state["updated_at"] = state.get("updated_at", "")

                await save_event_history(
                    loc_id=loc_id,
                    event=event_to_close,
                    close_reason="auto"
                )

                loc_name = LOCATIONS.get(loc_id, {}).get("name", loc_id)

                await save_unified_history(
                    loc_id=loc_id,
                    loc_name=loc_name,
                    category="incident",
                    action="auto_closed",
                    severity=event_to_close.get("type"),
                    title="Автозакрытие события",
                    description=event_to_close.get("comment") or "Без комментария",
                    source="system",
                    event_id=event_id,
                    close_reason="auto"
                )

                await send_event_to_matrix(
                    loc_id=loc_id,
                    event_type="normal",
                    comment=f"Автозакрытие: {event_to_close.get('comment', '')}",
                    author_user_id="system"
                )

                await set_state(loc_id, state)

                logger.info(f"[AUTO-CLOSE] Закрыто событие {event_id} в {loc_id}")

        except Exception as e:
            logger.error(f"[AUTO-CLOSE ERROR] {e}")

        await asyncio.sleep(30)