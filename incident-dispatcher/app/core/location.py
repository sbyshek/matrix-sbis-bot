import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.alert_svc import get_location_alerts
from app.core.config import settings
from app.core.history import save_unified_history
from app.core.matrix import send_event_to_matrix
from app.core.redis import (
    delete_event_ttl,
    get_event_history,
    get_state,
    save_event_history,
    set_event_ttl,
    set_state
)
from app.core.security import verify_ip
from app.core.subscribers import subscriber_key
from app.security import (
    LOCATIONS,
    MatrixUser,
    get_matrix_user_read,
    verify_location_access_or_dashboard
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["location"])


def get_effective_mode(events: list) -> str:
    if not events:
        return "normal"

    if any(e.get("type") == "alarm" for e in events):
        return "alarm"

    if any(e.get("type") == "attention" for e in events):
        return "attention"

    return "normal"


@router.get("/api/location/{loc_id}/status")
async def get_location_status(
    loc_id: str,
    user: MatrixUser = Depends(get_matrix_user_read)
):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    loc_data = LOCATIONS[loc_id]
    state = await get_state(loc_id)

    return {
        "loc_id": loc_id,
        "name": loc_data["name"],
        "icon": loc_data.get("icon", "📍"),
        "mode": get_effective_mode(state.get("events", [])),
        "events": state.get("events", []),
        "updated_at": state.get("updated_at", ""),
        "is_moderator": user.is_moderator,
        "user_id": user.user_id
    }


@router.post("/api/location/{loc_id}/event")
async def create_event(
    loc_id: str,
    request: Request,
    user: MatrixUser = Depends(verify_location_access_or_dashboard)
):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    loc = LOCATIONS[loc_id]

    body = await request.json()

    event_type = body.get("type")
    comment = body.get("comment", "")

    if event_type not in ("attention", "alarm"):
        raise HTTPException(status_code=400, detail="Invalid type")

    auto_close_at = None
    ttl_minutes = 0

    if event_type == "attention" and settings.AUTO_CLOSE_ATTENTION_MINUTES > 0:
        ttl_minutes = settings.AUTO_CLOSE_ATTENTION_MINUTES
    elif event_type == "alarm" and settings.AUTO_CLOSE_ALARM_MINUTES > 0:
        ttl_minutes = settings.AUTO_CLOSE_ALARM_MINUTES

    if ttl_minutes > 0:
        auto_close_at = (
            datetime.now() + timedelta(minutes=ttl_minutes)
        ).isoformat()

    event_id = str(uuid.uuid4())[:8]

    new_event = {
        "id": event_id,
        "type": event_type,
        "comment": comment,
        "timestamp": datetime.now().isoformat(),
        "source": user.user_id,
        "auto_close_at": auto_close_at
    }

    state = await get_state(loc_id)

    state.setdefault("events", []).append(new_event)
    state["updated_at"] = datetime.now().isoformat()

    await set_state(loc_id, state)

    if ttl_minutes > 0:
        await set_event_ttl(loc_id, event_id, ttl_minutes)

    await save_unified_history(
        loc_id=loc_id,
        loc_name=loc["name"],
        category="incident",
        action="created",
        severity=event_type,
        title="Тревога" if event_type == "alarm" else "Внимание",
        description=comment or "Без комментария",
        source=user.user_id,
        event_id=event_id
    )

    await send_event_to_matrix(
        loc_id=loc_id,
        event_type=event_type,
        comment=comment,
        author_user_id=user.user_id
    )

    return {
        "status": "success",
        "event_id": event_id,
        "mode": get_effective_mode(state["events"])
    }


@router.post("/api/location/{loc_id}/close")
async def close_event(
    loc_id: str,
    request: Request,
    user: MatrixUser = Depends(verify_location_access_or_dashboard)
):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    body = await request.json()
    event_id = body.get("event_id")

    if not event_id:
        raise HTTPException(status_code=400, detail="event_id is required")

    state = await get_state(loc_id)

    event_to_close = None

    for event in state.get("events", []):
        if event.get("id") == event_id:
            event_to_close = event
            break

    if not event_to_close:
        raise HTTPException(status_code=404, detail="Event not found")

    state["events"] = [
        event for event in state["events"]
        if event.get("id") != event_id
    ]

    await save_event_history(
        loc_id=loc_id,
        event=event_to_close,
        close_reason="manual"
    )

    await delete_event_ttl(loc_id, event_id)

    await save_unified_history(
        loc_id=loc_id,
        loc_name=LOCATIONS[loc_id]["name"],
        category="incident",
        action="closed",
        severity=event_to_close.get("type"),
        title="Событие закрыто",
        description=event_to_close.get("comment") or "Без комментария",
        source=user.user_id,
        event_id=event_id,
        close_reason="manual"
    )

    if not state["events"]:
        await save_unified_history(
            loc_id=loc_id,
            loc_name=LOCATIONS[loc_id]["name"],
            category="incident",
            action="normal",
            severity="normal",
            title="Все события закрыты",
            description="Активных событий нет",
            source=user.user_id
        )

        await send_event_to_matrix(
            loc_id=loc_id,
            event_type="normal",
            comment="Все инциденты закрыты",
            author_user_id=user.user_id
        )

    state["updated_at"] = datetime.now().isoformat()

    await set_state(loc_id, state)

    return {
        "status": "success",
        "mode": get_effective_mode(state["events"])
    }


@router.post("/api/location/{loc_id}/clear-all")
async def clear_all_events(
    loc_id: str,
    request: Request,
    user: MatrixUser = Depends(verify_location_access_or_dashboard)
):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    state = {
        "events": [],
        "updated_at": datetime.now().isoformat()
    }

    await set_state(loc_id, state)

    await save_unified_history(
        loc_id=loc_id,
        loc_name=LOCATIONS[loc_id]["name"],
        category="incident",
        action="cleared",
        severity="normal",
        title="Все события закрыты принудительно",
        description="Очистка всех событий",
        source=user.user_id
    )

    await send_event_to_matrix(
        loc_id=loc_id,
        event_type="normal",
        comment="Все инциденты закрыты принудительно",
        author_user_id=user.user_id
    )

    return {
        "status": "success",
        "mode": "normal"
    }


@router.get("/api/location/{loc_id}/history")
async def get_location_history(
    loc_id: str,
    user: MatrixUser = Depends(get_matrix_user_read)
):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    history = await get_event_history(loc_id)

    return {
        "loc_id": loc_id,
        "loc_name": LOCATIONS[loc_id]["name"],
        "events": history
    }


@router.get("/api/location/{loc_id}/subscribers")
async def get_location_subscribers(
    loc_id: str,
    template_id: str = None,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    token = x_local_token or x_api_token

    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    alerts = get_location_alerts(loc_id)

    if template_id:
        alerts = [
            alert for alert in alerts
            if alert.get("template_id") == template_id
        ]

    seen = {}

    for alert in alerts:
        for raw_sub in alert.get("subscribers", []):
            raw_sub = str(raw_sub).strip()

            if not raw_sub:
                continue

            key = subscriber_key(raw_sub)

            if key not in seen:
                seen[key] = raw_sub

    subscribers = list(seen.values())

    return {
        "loc_id": loc_id,
        "template_id": template_id,
        "total_count": len(subscribers),
        "subscribers": subscribers
    }