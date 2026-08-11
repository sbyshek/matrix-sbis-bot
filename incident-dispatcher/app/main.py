from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import redis.asyncio as redis
import json
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx
import markdown
import logging
from app.core.security import verify_local_token, verify_ip, verify_ui_access
from app.security import (
    verify_dashboard_access,
    verify_location_access_or_dashboard,
    get_matrix_user_read,
    get_matrix_user_write,
    get_location_events,
    get_template_by_id,
    MatrixUser,
    LOCATIONS, 
    check_ip_whitelist
    
)
from app.models import (
    EventCreate,
    EventClose,
    AsteriskTriggerRequest, 
    VoiceTriggerRequest,
    AlertTriggerRequest,
    BulkAlertTriggerRequest,
    DeskAlertTriggerRequest
)


from app.alert_svc import TEMPLATES, get_template_by_id, get_location_alerts, _subscriber_key

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Matrix Incident Dispatcher")
templates = Jinja2Templates(directory="templates")

redis_client = None

CAMPAIGN_HISTORY_KEY = "dispatch:campaign_history"
CAMPAIGN_HISTORY_LIMIT = settings.CAMPAIGN_HISTORY_LIMIT

UNIFIED_HISTORY_GLOBAL_KEY = "dispatch:unified_history"
UNIFIED_HISTORY_LOC_KEY_TEMPLATE = "dispatch:unified_history:{loc_id}"
UNIFIED_HISTORY_LIMIT = 1000


@app.on_event("startup")
async def startup_event():
    global redis_client
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    await init_locations_in_redis()
    asyncio.create_task(auto_close_checker())

async def init_locations_in_redis():
    for loc_id in LOCATIONS.keys():
        key = f"dispatch:state:{loc_id}"
        if not await redis_client.exists(key):
            await redis_client.set(key, json.dumps({"events": [], "updated_at": datetime.now().isoformat()}))

def get_effective_mode(events: list) -> str:
    if not events: return "normal"
    if any(e["type"] == "alarm" for e in events): return "alarm"
    if any(e["type"] == "attention" for e in events): return "attention"
    return "normal"


def verify_desk_access(
    request: Request,
    loc_id: str,
    x_api_token: Optional[str] = None
):
    """
    Проверка доступа к локальному пульту локации.

    Доступ разрешён:
    1. Если IP клиента входит в allowed_ips конкретной локации.
    2. Опционально: если передан глобальный DASHBOARD_API_TOKEN,
       и IP входит в DASHBOARD_ALLOWED_IPS.
    """
    loc = LOCATIONS.get(loc_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    # Опциональный доступ для глобального дашборда
    if x_api_token and x_api_token == settings.DASHBOARD_API_TOKEN:
        dashboard_ips = [
            ip.strip()
            for ip in settings.DASHBOARD_ALLOWED_IPS.split(",")
            if ip.strip()
        ]

        if check_ip_whitelist(request, dashboard_ips):
            logger.info(f"✅ Desk access granted via dashboard token for {loc_id}")
            return

    # Основной доступ: IP пульта из allowed_ips локации
    allowed_ips = loc.get("allowed_ips", [])

    if allowed_ips and check_ip_whitelist(request, allowed_ips):
        logger.info(f"✅ Desk access granted by allowed_ips for {loc_id}")
        return

    raise HTTPException(
        status_code=403,
        detail="Access denied: IP not allowed for this location desk"
    )


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
):
    """
    Единая история дашборда.

    category:
        - incident
        - campaign
        - system

    action:
        - created
        - closed
        - cleared
        - auto_closed
        - triggered
        - failed
    """
    now_utc = datetime.now(timezone.utc)

    try:
        local_tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        local_tz = ZoneInfo("Asia/Novosibirsk")

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
        global_key = UNIFIED_HISTORY_GLOBAL_KEY
        loc_key = UNIFIED_HISTORY_LOC_KEY_TEMPLATE.format(loc_id=loc_id)

        payload = json.dumps(record, ensure_ascii=False)

        pipe = redis_client.pipeline()

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


@app.get("/api/dashboard")
async def get_dashboard_state(
    request: Request,
    _=Depends(verify_dashboard_access)
):
    result = {}
    for loc_id, loc_data in LOCATIONS.items():
        state_raw = await redis_client.get(f"dispatch:state:{loc_id}")
        state = json.loads(state_raw) if state_raw else {"events": [], "updated_at": datetime.now().isoformat()}
        
        result[loc_id] = {
            "name": loc_data["name"],
            "icon": loc_data.get("icon", "📍"),
            "mode": get_effective_mode(state["events"]),
            "events": state["events"],
            "updated_at": state["updated_at"],
            "alerts": get_location_alerts(loc_id),
            "allowed_ips": loc_data.get("allowed_ips", [])
        }
    return result

@app.get("/api/location/{loc_id}/status")
async def get_location_status(
    loc_id: str,
    user: MatrixUser = Depends(get_matrix_user_read)
):
    """Публичный (для участников комнаты) эндпоинт чтения статуса"""
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    
    loc_data = LOCATIONS[loc_id]
    state_raw = await redis_client.get(f"dispatch:state:{loc_id}")
    state = json.loads(state_raw) if state_raw else {"events": [], "updated_at": datetime.now().isoformat()}
    
    return {
        "loc_id": loc_id,
        "name": loc_data["name"],
        "icon": loc_data.get("icon", "📍"),
        "mode": get_effective_mode(state["events"]),
        "events": state["events"],
        "updated_at": state["updated_at"],
        "is_moderator": user.is_moderator,
        "user_id": user.user_id
    }

@app.post("/api/location/{loc_id}/event")
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
        auto_close_at = (datetime.now() + timedelta(minutes=ttl_minutes)).isoformat()

    event_id = str(uuid.uuid4())[:8]
    new_event = {
        "id": event_id,
        "type": event_type,
        "comment": comment,
        "timestamp": datetime.now().isoformat(),
        "source": user.user_id,
        "auto_close_at": auto_close_at
    }
    
    key = f"dispatch:state:{loc_id}"
    state_raw = await redis_client.get(key)
    state = json.loads(state_raw) if state_raw else {"events": [], "updated_at": ""}
    
    state["events"].append(new_event)
    state["updated_at"] = datetime.now().isoformat()

    await redis_client.set(key, json.dumps(state, ensure_ascii=False))

    if ttl_minutes > 0:
        ttl_key = f"dispatch:ttl:{loc_id}:{event_id}"
        await redis_client.set(ttl_key, "1", ex=ttl_minutes * 60)

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
    await send_event_to_matrix(loc_id, event_type, comment, user.user_id)

    return {"status": "success", "event_id": event_id, "mode": get_effective_mode(state["events"])}

@app.post("/api/location/{loc_id}/close")
async def close_event(
    loc_id: str,
    request: Request,
    user: MatrixUser = Depends(verify_location_access_or_dashboard)
):
    body = await request.json()
    event_id = body.get("event_id")
    if not event_id:
        raise HTTPException(status_code=400, detail="event_id is required")

    key = f"dispatch:state:{loc_id}"
    state_raw = await redis_client.get(key)
    if not state_raw:
        raise HTTPException(status_code=404, detail="Location state not found")
        
    state = json.loads(state_raw)
    event_to_close = None
    for e in state["events"]:
        if e["id"] == event_id:
            event_to_close = e
            break
    
    if not event_to_close:
        raise HTTPException(status_code=404, detail="Event not found")
    
    state["events"] = [e for e in state["events"] if e["id"] != event_id]

    
    # await save_to_history(loc_id, event_to_close, "manual")

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
    
    await redis_client.delete(f"dispatch:ttl:{loc_id}:{event_id}")
    
    # if not state["events"]:
    #     await send_to_matrix(loc_id, "normal", "Все инциденты закрыты", "system")
    
    
    state["updated_at"] = datetime.now().isoformat()
    await redis_client.set(key, json.dumps(state, ensure_ascii=False))

    if not state["events"]:
        await send_event_to_matrix(
            loc_id,
            "normal",
            "Все инциденты закрыты",
            user.user_id
        )

    return {"status": "success", "mode": get_effective_mode(state["events"])}
    

@app.post("/api/location/{loc_id}/clear-all")
async def clear_all_events(
    loc_id: str,
    request: Request,
    user: MatrixUser = Depends(verify_location_access_or_dashboard)
):
    key = f"dispatch:state:{loc_id}"
    state = {"events": [], "updated_at": datetime.now().isoformat()}
    await redis_client.set(key, json.dumps(state, ensure_ascii=False))

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
        loc_id,
        "normal",
        "Все инциденты закрыты принудительно",
        user.user_id
    )

    return {"status": "success", "mode": "normal"}

@app.get("/api/location/{loc_id}/history")
async def get_location_history(
    loc_id: str,
    user: MatrixUser = Depends(get_matrix_user_read)
):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    
    history_key = f"dispatch:history:{loc_id}"
    history_raw = await redis_client.get(history_key)
    history = json.loads(history_raw) if history_raw else []
    
    return {
        "loc_id": loc_id,
        "loc_name": LOCATIONS[loc_id]["name"],
        "events": history
    }

# --- Matrix отправка ---

# =============================================================================
# Matrix отправка
# =============================================================================

async def _send_matrix_message(room_id: str, message: str) -> bool:
    """
    Низкоуровневая отправка сообщения в Matrix комнату.
    """
    if not room_id:
        logger.error("❌ Matrix: room_id is empty")
        return False

    html_body = markdown.markdown(message, extensions=['nl2br'])

    payload = {
        "msgtype": "m.text",
        "body": message,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body
    }

    txn_id = f"msg_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:4]}"
    url = (
        f"{settings.MATRIX_HOMESERVER}"
        f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.put(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {settings.MATRIX_ACCESS_TOKEN}"}
            )

            if resp.status_code in (200, 201):
                logger.info(f"✅ Matrix message sent to room {room_id}")
                return True

            logger.error(f"❌ Matrix API error {resp.status_code}: {resp.text}")
            return False

        except Exception as e:
            logger.error(f"❌ Matrix connection error: {e}")
            return False


async def send_event_to_matrix(
    loc_id: str,
    event_type: str,
    comment: str,
    author_user_id: str
) -> bool:
    """
    Отправляет событие диспетчера:
    - normal
    - attention
    - alarm
    """
    loc = LOCATIONS.get(loc_id)

    if not loc:
        logger.error(f"❌ Matrix: location {loc_id} not found")
        return False

    room_id = loc.get("room_id")

    if not room_id:
        logger.error(f"❌ Matrix: location {loc_id} has no room_id")
        return False

    tz = ZoneInfo(settings.TIMEZONE)
    time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")

    if event_type == "attention":
        template = settings.TEMPLATE_ATTENTION.replace("\n", "<br>")
    elif event_type == "alarm":
        template = settings.TEMPLATE_ALARM.replace("\n", "<br>")
    else:
        template = settings.TEMPLATE_NORMAL.replace("\n", "<br>")

    message = template.replace("{location}", loc.get("name", loc_id))
    message = message.replace("{comment}", comment or "Без комментария")
    message = message.replace("{time}", time_str)
    message = message.replace("{author}", author_user_id or "system")

    return await _send_matrix_message(room_id, message)


async def send_alert_to_matrix(
    loc_id: str,
    template_id: str,
    author_user_id: str
) -> bool:
    """
    Отправляет сообщение о запуске сценария оповещения.
    Берёт matrix_text из locations.json -> alerts[].matrix_text.
    """
    loc = LOCATIONS.get(loc_id)

    if not loc:
        logger.error(f"❌ Matrix: location {loc_id} not found")
        return False

    room_id = loc.get("room_id")

    if not room_id:
        logger.error(f"❌ Matrix: location {loc_id} has no room_id")
        return False

    alert_template = next(
        (
            alert for alert in loc.get("alerts", [])
            if alert.get("template_id") == template_id
        ),
        None
    )

    if not alert_template:
        logger.error(
            f"❌ Matrix: alert template {template_id} not configured for {loc_id}"
        )
        return False

    tz = ZoneInfo(settings.TIMEZONE)
    time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")

    message = alert_template.get("matrix_text", "")

    if not message:
        logger.error(
            f"❌ Matrix: matrix_text is empty for {loc_id}/{template_id}"
        )
        return False

    message = message.replace("{time}", time_str)
    message = message.replace("{author}", author_user_id or "система")

    return await _send_matrix_message(room_id, message)


async def send_to_matrix(
    loc_id: str,
    template_id: str,
    author_user_id: str
) -> bool:
    """
    Оставлено для совместимости со старыми вызовами.
    Теперь это просто alias для send_alert_to_matrix.
    """
    return await send_alert_to_matrix(loc_id, template_id, author_user_id)


# async def save_to_history(loc_id: str, event: dict, close_reason: str):
#     history_key = f"dispatch:history:{loc_id}"
#     event["closed_at"] = datetime.now().isoformat()
#     event["close_reason"] = close_reason
    
#     history_raw = await redis_client.get(history_key)
#     history = json.loads(history_raw) if history_raw else []
#     history.insert(0, event)
#     history = history[:100]
    
#     ttl_seconds = settings.HISTORY_TTL_HOURS * 3600
#     await redis_client.set(history_key, json.dumps(history, ensure_ascii=False), ex=ttl_seconds)

async def save_campaign_history(
    *,
    loc_id: str,
    loc_name: str,
    template_id: str,
    template_name: str,
    severity: str,
    campaign_id: str,
    subscribers_count: int,
    initiator: str,
    source: str
) -> dict | None:
    """
    Сохраняет запись о запущенном оповещении в Redis.

    Формат записи:
    - время UTC;
    - время локальное UTC+7;
    - локация;
    - сценарий;
    - campaign_id.
    """
    if not campaign_id:
        logger.warning("⚠️ Campaign ID is empty, skip campaign history saving")
        return None

    now_utc = datetime.now(timezone.utc)

    try:
        local_tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        local_tz = ZoneInfo("Asia/Novosibirsk")

    record = {
        "record_id": str(uuid.uuid4())[:8],
        "started_at_utc": now_utc.isoformat(),
        "started_at_local": now_utc.astimezone(local_tz).strftime("%d.%m.%Y %H:%M:%S"),
        "timezone": str(local_tz),
        "loc_id": loc_id,
        "loc_name": loc_name,
        "template_id": template_id,
        "template_name": template_name,
        "severity": severity,
        "campaign_id": campaign_id,
        "subscribers_count": subscribers_count,
        "initiator": initiator,
        "source": source
    }

    try:
        pipe = redis_client.pipeline()
        pipe.lpush(CAMPAIGN_HISTORY_KEY, json.dumps(record, ensure_ascii=False))
        pipe.ltrim(CAMPAIGN_HISTORY_KEY, 0, CAMPAIGN_HISTORY_LIMIT - 1)
        await pipe.execute()

        logger.info(
            f"✅ Campaign history saved: {campaign_id} | "
            f"{loc_name} | {template_name} | source={source}"
        )

        return record

    except Exception as e:
        logger.error(f"❌ Failed to save campaign history: {e}")
        return None


async def auto_close_checker():
    while True:
        try:
            ttl_keys = await redis_client.keys("dispatch:ttl:*")
            for ttl_key in ttl_keys:
                exists = await redis_client.exists(ttl_key)
                if not exists:
                    parts = ttl_key.split(":")
                    if len(parts) == 4:
                        loc_id = parts[2]
                        event_id = parts[3]
                        
                        key = f"dispatch:state:{loc_id}"
                        state_raw = await redis_client.get(key)
                        if state_raw:
                            state = json.loads(state_raw)
                            event_to_close = None
                            for e in state["events"]:
                                if e["id"] == event_id:
                                    event_to_close = e
                                    break
                            
                            if event_to_close:
                                state["events"] = [e for e in state["events"] if e["id"] != event_id]
                                # await save_to_history(loc_id, event_to_close, "auto")
                                await save_unified_history(
                                    loc_id=loc_id,
                                    loc_name=LOCATIONS.get(loc_id, {}).get("name", loc_id),
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
                                    loc_id,
                                    "normal",
                                    f"Автозакрытие: {event_to_close.get('comment', '')}",
                                    "system"
                                )

                                state["updated_at"] = datetime.now().isoformat()
                                await redis_client.set(key, json.dumps(state, ensure_ascii=False))
        except Exception as e:
            print(f"[AUTO-CLOSE ERROR] {e}")
        
        await asyncio.sleep(30)

# --- Web Pages ---

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "dashboard_token": settings.DASHBOARD_API_TOKEN
        }
        )

@app.get("/widget/{loc_id}", response_class=HTMLResponse)
async def location_widget(request: Request, loc_id: str):
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    
    loc = LOCATIONS[loc_id]
    
    return templates.TemplateResponse("location.html", {
        "request": request,
        "loc_id": loc_id,
        "loc_name": loc["name"],
        "loc_icon": loc.get("icon", "📍"),
        "room_id": loc["room_id"]
    })

@app.post("/api/v1/incident/asterisk")
async def trigger_from_asterisk_voice(
    request_data: AsteriskTriggerRequest,
    x_api_token: str = Header(..., alias="X-API-Token")
):
    """Эндпоинт для приема транскрибированного голоса от voice-api"""
    
    # 1. Проверка безопасности (внутренний токен между сервисами)
    expected_token = settings.ASTERISK_API_TOKEN
    if x_api_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid X-API-Token")

    loc_id = request_data.loc_id
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail=f"Location {loc_id} not found")

    # 2. Создаем событие (логика дублирует create_event, но адаптирована под голос)
    event_id = str(uuid.uuid4())[:8]
    new_event = {
        "id": event_id,
        "type": "alarm", # Голосовое сообщение всегда считаем тревогой/вниманием высокого приоритета
        "comment": f"🎙️ [Голос от {request_data.caller_id}]: {request_data.comment}",
        "timestamp": datetime.now().isoformat(),
        "source": request_data.source
    }
    
    key = f"dispatch:state:{loc_id}"
    state_raw = await redis_client.get(key)
    state = json.loads(state_raw) if state_raw else {"events": [], "updated_at": ""}
    
    state["events"].append(new_event)
    state["updated_at"] = datetime.now().isoformat()
    await redis_client.set(key, json.dumps(state, ensure_ascii=False))
    
    # 3. Отправляем в Matrix через нашу проверенную функцию
    await send_to_matrix(loc_id, "alarm", new_event["comment"], "Asterisk Voice Bot")
    
    logger.info(f"✅ Incident created from voice: {loc_id}, event_id: {event_id}")
    
    return {"status": "success", "event_id": event_id}


# @app.post("/api/v1/alert/trigger")
# async def trigger_alert_template(
#     request_data: AlertTriggerRequest,
#     request: Request,
#     x_local_token: str = Header(..., alias="X-Local-Token"),
#     x_api_token: str = Header(default=None, alias="X-API-Token")
# ):
#     """Ручной запуск шаблона голосового оповещения из UI диспетчера"""
#     # from app.core.security import verify_local_token, verify_ip
    
#     # 1. Безопасность
#     # verify_local_token(x_local_token)
    
#     logger.info(f"✅ Triggering alert template: {request_data.template_id}")
#     logger.info(f"🔑 X-Local-Token: {x_local_token}")
#     logger.info(f"🔑 X-API-Token: {x_api_token}")
    
#     token = x_local_token or x_api_token
#     if not token:
#         raise HTTPException(status_code=403, detail="Требуется токен авторизации")
    
#     if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
#         raise HTTPException(status_code=403, detail="Неверный токен")
    
#     verify_ip(request)
    
#     # 2. Поиск локации в ЕДИНОМ конфиге
#     loc_id = request_data.loc_id
#     loc_data = LOCATIONS.get(loc_id)
#     if not loc_data:
#         raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found")
    
#     # 3. Поиск шаблона внутри локации
#     alert_template = None
#     for alert in loc_data.get("alerts", []):
#         if alert["template_id"] == request_data.template_id:
#             alert_template = alert
#             break
            
#     if not alert_template:
#         raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not configured for '{loc_id}'")
    
#     subscribers = alert_template.get("subscribers", [])
#     if not subscribers:
#         raise HTTPException(status_code=400, detail="No subscribers configured")
    
#     # 4. Запуск голосового обзвона
#     try:
#         async with httpx.AsyncClient(timeout=10.0) as client:
#             payload = {
#                 "node_id": loc_data.get("asterisk_node", "zv-asterisk"),
#                 "template_id": alert_template["template_id"],
#                 "audio_file": alert_template.get("audio_file", "custom/default_8000"), # Убедись, что это поле есть в конфиге, если нужно
#                 "text_for_matrix": alert_template["matrix_text"],
#                 "subscribers": subscribers,
#                 "loc_id": loc_id,
#                 "loc_name": loc_data["name"],
#                 "initiator": "dashboard_user"
#             }
            
#             # Если audio_file нет в новом конфиге, добавь его туда или используй дефолтный
#             if "audio_file" not in alert_template:
#                 payload["audio_file"] = f"custom/{alert_template['template_id']}_8000"
            
#             response = await client.post(
#                 f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
#                 json=payload,
#                 headers={"X-Secret": settings.VOICE_API_SECRET}
#             )
            
#             if response.status_code != 200:
#                 raise HTTPException(status_code=502, detail=f"voice-api error: {response.text}")
#             result = response.json()
            
#     except httpx.RequestError as e:
#         raise HTTPException(status_code=503, detail=f"voice-api unavailable: {str(e)}")
    
#     # 5. ОТПРАВКА В MATRIX (ТЕПЕРЬ ТОЧНО СРАБОТАЕТ)
#     await send_to_matrix(loc_id, alert_template["template_id"], "dashboard_user")
    
#     return {
#         "status": "success",
#         "template": alert_template["name"],
#         "location": loc_data["name"],
#         "campaign_id": result.get("campaign_id")
#     }



# @app.post("/api/v1/alert/trigger")
# async def trigger_alert_template(
#     request_data: AlertTriggerRequest,
#     request: Request,
#     x_local_token: str = Header(default=None, alias="X-Local-Token"),
#     x_api_token: str = Header(default=None, alias="X-API-Token")
# ):
#     """Ручной запуск шаблона голосового оповещения из UI диспетчера"""
#     from app.core.security import verify_ip
    
#     # 1. Проверка безопасности: принимаем либо X-Local-Token, либо X-API-Token
#     token = x_local_token or x_api_token
#     if not token:
#         raise HTTPException(status_code=403, detail="Требуется токен авторизации")
    
#     if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
#         raise HTTPException(status_code=403, detail="Неверный токен")
        
#     verify_ip(request)
    
#     # 2. Находим локацию
#     loc_id = request_data.loc_id
#     if loc_id not in LOCATIONS:
#         raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found")
#     loc_data = LOCATIONS[loc_id]
    
#     # 3. Находим шаблон
#     template = get_template_by_id(request_data.template_id)
#     if not template:
#         raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not found")
    
#     # 4. Находим событие для этой локации
#     location_events = get_location_events(loc_id)
#     event_config = next((ev for ev in location_events if ev["template_id"] == template["id"]), None)
    
#     if not event_config:
#         raise HTTPException(
#             status_code=404,
#             detail=f"Template '{template['name']}' not configured for location '{loc_data['name']}'"
#         )
    
#     # 5. Формируем список абонентов
#     subscribers = event_config.get("subscribers", [])
#     if not subscribers:
#         raise HTTPException(status_code=400, detail="No subscribers configured for this alert")
    
#     # 6. Отправляем в voice-api
#     try:
#         async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
#             payload = {
#                 "node_id": loc_data.get("asterisk_node", "zv-asterisk"),
#                 "template_id": template["id"],
#                 "audio_file": template["audio_file"],
#                 "text_for_matrix": template["text_for_matrix"],
#                 "subscribers": subscribers,
#                 "loc_id": loc_id,
#                 "loc_name": loc_data["name"],
#                 "initiator": "dashboard_user"
#             }
#             response = await client.post(
#                 f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
#                 json=payload,
#                 headers={"X-Secret": settings.VOICE_API_SECRET}
#             )
#             if response.status_code != 200:
#                 logger.error(f"❌ voice-api returned {response.status_code}: {response.text}")
#                 raise HTTPException(status_code=502, detail=f"voice-api error: {response.text}")
#             result = response.json()
#     except httpx.RequestError as e:
#         logger.error(f"❌ Failed to connect to voice-api: {e}")
#         raise HTTPException(status_code=503, detail=f"voice-api unavailable: {str(e)}")
    
#     logger.info(f"✅ Alert triggered: {template['name']} for {loc_data['name']}")
    
#     return {
#         "status": "success",
#         "template": template["name"],
#         "location": loc_data["name"],
#         "subscribers_count": len(subscribers),
#         "campaign_id": result.get("campaign_id"),
#         "voice_api_response": result
#     }


@app.post("/api/v1/alert/trigger")
async def trigger_alert_template(
    request_data: AlertTriggerRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    """Ручной запуск шаблона голосового оповещения из UI диспетчера"""
    from app.core.security import verify_ip
    
    # Проверка токена
    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")
    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    verify_ip(request)
    
    # Находим локацию
    loc_id = request_data.loc_id
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found")
    loc_data = LOCATIONS[loc_id]
    
    # Находим шаблон
    template = get_template_by_id(request_data.template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not found")
    
    # Находим событие для этой локации
    location_alerts = get_location_alerts(loc_id)
    alert_config = next((a for a in location_alerts if a["template_id"] == template["id"]), None)
    
    if not alert_config:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template['name']}' not configured for location '{loc_data['name']}'"
        )
    
    # Формируем список абонентов
    subscribers = alert_config.get("subscribers", [])
    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured for this alert")
    
    # Отправляем в voice-api
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "node_id": loc_data.get("asterisk_node", "zv-asterisk"),
                "template_id": template["id"],
                "audio_file": template["audio_file"],
                "text_for_matrix": template.get("default_text", ""),
                "subscribers": subscribers,
                "loc_id": loc_id,
                "loc_name": loc_data["name"],
                "initiator": "dashboard_user"
            }
            
            response = await client.post(
                f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
                json=payload,
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )
            
            if response.status_code != 200:
                logger.error(f"❌ voice-api returned {response.status_code}: {response.text}")
                raise HTTPException(status_code=502, detail=f"voice-api error: {response.text}")
                
            result = response.json()
            
            await save_unified_history(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                category="campaign",
                action="triggered",
                severity=template.get("severity", "attention"),
                title=template.get("name", template["id"]),
                description=alert_config.get("text_for_matrix") or template.get("default_text", ""),
                source="dashboard_user",
                template_id=template["id"],
                template_name=template.get("name", template["id"]),
                campaign_id=result.get("campaign_id"),
                subscribers_count=len(subscribers)
            )
            
            await send_alert_to_matrix(
                loc_id,
                template["id"],
                "dashboard_user"
            )
            
            campaign_id = result.get("campaign_id")
            if campaign_id:
                await save_campaign_history(
                    loc_id=loc_id,
                    loc_name=loc_data["name"],
                    template_id=template["id"],
                    template_name=template.get("name", template["id"]),
                    severity=template.get("severity", "attention"),
                    campaign_id=campaign_id,
                    subscribers_count=len(subscribers),
                    initiator="dashboard_user",
                    source="dashboard"
                )

    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail=f"voice-api unavailable: {str(e)}")
    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail=f"voice-api unavailable: {str(e)}")
    
    logger.info(f"✅ Alert triggered: {template['name']} for {loc_data['name']}")
    
    return {
        "status": "success",
        "template": template["name"],
        "location": loc_data["name"],
        "subscribers_count": len(subscribers),
        "campaign_id": result.get("campaign_id"),
        "voice_api_response": result
    }



# =============================================================================
# IVR-запуск оповещения по коду с телефона
# =============================================================================
class AlertTriggerByCodeRequest(BaseModel):
    code: str
    caller: str  # Номер, с которого позвонили

# @app.post("/api/v1/alert/trigger-by-code")
# async def trigger_alert_by_code(
#     request_data: AlertTriggerByCodeRequest,
#     x_local_token: str = Header(..., alias="X-Local-Token"),
#     request: Request = None
# ):
#     """Запуск оповещения по коду с телефона (IVR)"""
#     verify_local_token(x_local_token)
#     verify_ip(request)
    
#     # Ищем шаблон по коду во всех локациях
#     template = None
#     loc_config = None
#     loc_id = None
    
#     for lid, events in ALERT_CONFIG.get("location_events", {}).items():
#         for event in events:
#             if event.get("trigger_code") == request_data.code:
#                 template = get_template_by_id(event["template_id"])
#                 loc_id = lid
#                 loc_config = LOCATIONS.get(lid)
#                 subscribers = event.get("subscribers", [])
#                 break
#         if template:
#             break
    
#     if not template or not loc_config:
#         raise HTTPException(status_code=404, detail=f"Alert code '{request_data.code}' not found")
    
#     if not subscribers:
#         raise HTTPException(status_code=400, detail="No subscribers configured")
    
#     # Отправляем в voice-api
#     try:
#         async with httpx.AsyncClient(timeout=10.0) as client:
#             payload = {
#                 "node_id": loc_config.get("asterisk_node", "zv-astreisk"),
#                 "template_id": template["id"],
#                 "audio_file": template["audio_file"],
#                 "text_for_matrix": template["text_for_matrix"],
#                 "subscribers": subscribers,
#                 "loc_id": loc_id,
#                 "loc_name": loc_config["name"],
#                 "initiator": f"ivr_{request_data.caller}"
#             }
            
#             response = await client.post(
#                 f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
#                 json=payload,
#                 headers={"X-Secret": settings.VOICE_API_SECRET}
#             )
            
#             if response.status_code != 200:
#                 raise HTTPException(status_code=502, detail="voice-api error")
            
#             voice_result = response.json()
            
#     except httpx.RequestError as e:
#         logger.error(f"❌ Failed to connect to voice-api: {e}")
#         raise HTTPException(status_code=503, detail="voice-api unavailable")
    
#     # Отправляем в Matrix
#     await send_to_matrix(loc_id, template["severity"], template["text_for_matrix"], f"IVR: {request_data.caller}")
    
#     logger.info(f"✅ IVR Alert triggered: {template['name']} for {loc_config['name']} by {request_data.caller}")
    
#     return {
#         "status": "success",
#         "template": template["name"],
#         "location": loc_config["name"],
#         "campaign_id": voice_result.get("campaign_id"),
#         "triggered_by": request_data.caller
#     }


# =============================================================================
# Получение деталей кампании из voice-api (для UI)
# =============================================================================
@app.get("/api/v1/campaign/{campaign_id}/details")
async def get_campaign_details(
    campaign_id: str,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Получает детали кампании из voice-api"""
    # verify_local_token(x_local_token)
    
    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    verify_ip(request)
    
    try:
        async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
            response = await client.get(
                f"{settings.VOICE_API_URL}/api/v1/campaign/{campaign_id}/logs",
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to fetch campaign details")
            
            return response.json()
            
    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail="voice-api unavailable")



# =============================================================================
# МАССОВОЕ ОПОВЕЩЕНИЕ 
# =============================================================================


@app.post("/api/v1/alert/trigger-bulk")
async def trigger_bulk_alert(
    request_data: BulkAlertTriggerRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    """Массовый запуск одного шаблона на несколько локаций"""
    from app.core.security import verify_ip
    
    # Проверка токена (как в одиночном trigger)
    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")
    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    verify_ip(request)
    
    # Находим шаблон
    template = get_template_by_id(request_data.template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not found")
    
    # Запускаем кампании для каждой локации параллельно
    results = {"success": [], "failed": [], "total": len(request_data.loc_ids)}
    
    async def trigger_single(loc_id: str):
        try:
            if loc_id not in LOCATIONS:
                return {"loc_id": loc_id, "error": f"Location '{loc_id}' not found"}

            loc_data = LOCATIONS[loc_id]

            location_alerts = get_location_alerts(loc_id)
            alert_config = next(
                (a for a in location_alerts if a["template_id"] == template["id"]),
                None
            )

            if not alert_config:
                return {
                    "loc_id": loc_id,
                    "error": f"Template not configured for '{loc_id}'"
                }

            subscribers = alert_config.get("subscribers", [])

            if not subscribers:
                return {"loc_id": loc_id, "error": "No subscribers"}

            async with httpx.AsyncClient(timeout=60.0) as client:
                payload = {
                    "node_id": loc_data.get("asterisk_node", "zv-asterisk"),
                    "template_id": template["id"],
                    "audio_file": alert_config.get("audio_file") or template.get("audio_file", ""),
                    "text_for_matrix": alert_config.get("text_for_matrix") or template.get("default_text", ""),
                    "subscribers": subscribers,
                    "loc_id": loc_id,
                    "loc_name": loc_data["name"],
                    "initiator": "bulk_dispatcher"
                }

                response = await client.post(
                    f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
                    json=payload,
                    headers={"X-Secret": settings.VOICE_API_SECRET}
                )

                if response.status_code != 200:
                    return {"loc_id": loc_id, "error": f"voice-api error: {response.text}"}

                result = response.json()

            
            await save_unified_history(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                category="campaign",
                action="triggered",
                severity=template.get("severity", "attention"),
                title=template.get("name", template["id"]),
                description=alert_config.get("text_for_matrix") or template.get("default_text", ""),
                source="bulk_dispatcher",
                template_id=template["id"],
                template_name=template.get("name", template["id"]),
                campaign_id=result.get("campaign_id"),
                subscribers_count=len(subscribers)
            )
            
            await send_alert_to_matrix(
                loc_id,
                template["id"],
                "bulk_dispatcher"
            )

            return {
                "loc_id": loc_id,
                "loc_name": loc_data["name"],
                "campaign_id": result.get("campaign_id"),
                "subscribers_count": len(subscribers)
            }

        except Exception as e:
            return {"loc_id": loc_id, "error": str(e)}
    
    # Параллельный запуск всех кампаний
    tasks = [trigger_single(loc_id) for loc_id in request_data.loc_ids]
    campaign_results = await asyncio.gather(*tasks)
    
    for result in campaign_results:
        if "error" in result:
            results["failed"].append(result)
        else:
            results["success"].append(result)
    
    logger.info(f"✅ Bulk alert: {template['name']} on {len(results['success'])}/{len(request_data.loc_ids)} locations")
    
    return results

# =============================================================================
# Список локаций с доступными шаблонами (для UI)
# =============================================================================
# @app.get("/api/v1/locations")
# async def get_locations(
#     x_local_token: str = Header(..., alias="X-Local-Token"),
#     x_api_token: str = Header(default=None, alias="X-API-Token"),
#     request: Request = None
# ):
#     """Возвращает список локаций с доступными шаблонами оповещений"""
    
#     verify_ui_access(request, x_local_token, x_api_token)
   
#     locations = []
#     for loc_id, loc_config in LOCATIONS.items():
#         alerts = []
#         location_events = ALERT_CONFIG.get("location_events", {}).get(loc_id, [])
        
#         for event in location_events:
#             template = get_template_by_id(event["template_id"])
#             if template:
#                 alerts.append({
#                     "template_id": template["id"],
#                     "name": template["name"],
#                     "severity": template["severity"],
#                     "trigger_code": event.get("trigger_code")
#                 })
        
#         locations.append({
#             "id": loc_id,
#             "name": loc_config["name"],
#             "icon": loc_config.get("icon", "📍"),
#             "alerts": alerts
#         })
    
#     return {"locations": locations}


@app.get("/api/v1/alerts/history")
async def get_alerts_history(
    limit: int = 100,
    loc_id: Optional[str] = None,
    template_id: Optional[str] = None,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """
    Возвращает историю запущенных оповещений из Redis.

    Поддерживает фильтры:
    - loc_id
    - template_id
    - limit
    """
    from app.core.security import verify_ip

    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)

    limit = max(1, min(limit, CAMPAIGN_HISTORY_LIMIT))

    raw_items = await redis_client.lrange(CAMPAIGN_HISTORY_KEY, 0, CAMPAIGN_HISTORY_LIMIT - 1)

    history = []

    for raw in raw_items:
        try:
            item = json.loads(raw)
        except Exception:
            continue

        if loc_id and item.get("loc_id") != loc_id:
            continue

        if template_id and item.get("template_id") != template_id:
            continue

        history.append(item)

        if len(history) >= limit:
            break

    return {
        "history": history,
        "count": len(history),
        "limit": limit
    }

@app.get("/api/v1/locations")
async def get_locations(
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Возвращает список локаций с доступными шаблонами оповещений"""
    from app.core.security import verify_ip
    
    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")
    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    verify_ip(request)
    
    locations = []
    for loc_id, loc_config in LOCATIONS.items():
        locations.append({
            "id": loc_id,
            "name": loc_config["name"],
            "icon": loc_config.get("icon", "📍"),
            "alerts": get_location_alerts(loc_id),
            "allowed_ips": loc_config.get("allowed_ips", [])
        })
    
    return {"locations": locations}


@app.exception_handler(404)
async def custom_404(request: Request, exc):
    return templates.TemplateResponse(
        "404.html",
        {"request": request, "message": "Запрашиваемый ресурс не найден"},
        status_code=404
    )
    
    
# =============================================================================
# Управление шаблонами оповещений
# =============================================================================
# @app.get("/api/v1/templates")
# async def get_templates(
#     x_local_token: str = Header(..., alias="X-Local-Token"),
#     x_api_token: str = Header(default=None, alias="X-API-Token"),
#     request: Request = None
# ):
#     """Возвращает список всех шаблонов оповещений"""
#     # verify_local_token(x_local_token)
#     # verify_ip(request)
    
#     verify_ui_access(request, x_local_token, x_api_token)
    
#     return {
#         "templates": ALERT_CONFIG.get("audio_templates", [])
#     }

# =============================================================================
# Управление шаблонами оповещений (обновлено для новой структуры)
# =============================================================================
# @app.get("/api/v1/templates")
# async def get_templates(
#     x_local_token: str = Header(default=None, alias="X-Local-Token"),
#     x_api_token: str = Header(default=None, alias="X-API-Token"),
#     request: Request = None
# ):
#     """Возвращает список всех шаблонов оповещений"""
#     from app.core.security import verify_ip
    
#     token = x_local_token or x_api_token
#     if not token:
#         raise HTTPException(status_code=403, detail="Token required")
#     if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
#         raise HTTPException(status_code=403, detail="Invalid token")
#     verify_ip(request)
    
#     # Возвращаем шаблоны в формате списка (для UI)
#     templates_list = [
#         {"id": tpl_id, **tpl_data}
#         for tpl_id, tpl_data in TEMPLATES.items()
#     ]
    
#     return {"templates": templates_list}


@app.get("/api/v1/templates")
async def get_templates(
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """
    Возвращает глобальные шаблоны оповещений из config/templates.json.

    Важно:
    - default_text берётся именно из templates.json;
    - text_for_matrix из locations.json здесь не используется.
    """
    from app.core.security import verify_ip

    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)

    templates_list = []
    for tpl_id, tpl_data in TEMPLATES.items():
        templates_list.append({
            "id": tpl_id,
            "name": tpl_data.get("name", tpl_id),
            "severity": tpl_data.get("severity", "attention"),
            "audio_file": tpl_data.get("audio_file", ""),
            "default_text": tpl_data.get("default_text", ""),
        })

    return {"templates": templates_list}


@app.post("/api/v1/templates")
async def create_or_update_template(
    template_data: dict,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    request: Request = None
):
    """Создаёт или обновляет шаблон оповещения"""
    from app.core.security import verify_ip
    
    verify_local_token(x_local_token)
    verify_ip(request)
    
    template_id = template_data.get("id")
    if not template_id:
        raise HTTPException(status_code=400, detail="Template ID is required")
    
    # Обновляем в памяти
    TEMPLATES[template_id] = {
        "name": template_data.get("name", ""),
        "audio_file": template_data.get("audio_file", ""),
        "severity": template_data.get("severity", "attention"),
        "default_text": template_data.get("default_text", "")
    }
    
    # Сохраняем в файл
    try:
        with open("config/templates.json", "w", encoding="utf-8") as f:
            json.dump(TEMPLATES, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ Template saved: {template_id}")
        return {"status": "success", "template_id": template_id}
    except Exception as e:
        logger.error(f"❌ Failed to save template: {e}")
        raise HTTPException(status_code=500, detail="Failed to save template")


@app.delete("/api/v1/templates/{template_id}")
async def delete_template(
    template_id: str,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    request: Request = None
):
    """Удаляет шаблон оповещения"""
    from app.core.security import verify_ip
    
    verify_local_token(x_local_token)
    verify_ip(request)
    
    if template_id not in TEMPLATES:
        raise HTTPException(status_code=404, detail="Template not found")
    
    del TEMPLATES[template_id]
    
    try:
        with open("config/templates.json", "w", encoding="utf-8") as f:
            json.dump(TEMPLATES, f, ensure_ascii=False, indent=2)
        logger.info(f"️ Template deleted: {template_id}")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to delete template")



# =============================================================================
# История кампаний
# =============================================================================
@app.get("/api/v1/campaigns/history")
async def get_campaigns_history(
    limit: int = 50,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Возвращает историю кампаний из JSONL файлов voice-api"""
    # verify_local_token(x_local_token)
    # verify_ip(request)
    
    verify_ui_access(request, x_local_token, x_api_token)
    
    # Читаем JSONL файл из voice-api (через прямой доступ к файлу или через API)
    # Для простоты — читаем локальный файл, если он смонтирован
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.VOICE_API_URL}/api/v1/campaigns/list?limit={limit}",
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="voice-api error")
            
            return response.json()
            
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail="voice-api unavailable")
    
    
    
@app.get("/api/location/{loc_id}/subscribers")
async def get_location_subscribers(
    loc_id: str,
    template_id: Optional[str] = None,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """
    Возвращает список уникальных абонентов для локации.

    Если указан template_id,返回 абонентов только для выбранного шаблона.
    Если template_id не указан,返回 всех абонентов по всем алертам локации.
    """
    from app.core.security import verify_ip

    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    # Берём алерты локации из нового locations.json через alert_svc
    alerts = get_location_alerts(loc_id)

    # Если выбран конкретный шаблон — фильтруем по нему
    if template_id:
        alerts = [
            alert for alert in alerts
            if alert.get("template_id") == template_id
        ]

    # Собираем уникальных абонентов
    seen = {}

    for alert in alerts:
        for raw_sub in alert.get("subscribers", []):
            raw_sub = str(raw_sub).strip()
            if not raw_sub:
                continue

            key = _subscriber_key(raw_sub)

            if key not in seen:
                seen[key] = raw_sub

    subscribers = list(seen.values())

    return {
        "loc_id": loc_id,
        "template_id": template_id,
        "total_count": len(subscribers),
        "subscribers": subscribers
    }
    
@app.get("/desk/{loc_id}", response_class=HTMLResponse)
async def location_desk_page(request: Request, loc_id: str):
    """
    HTML-страница локального пульта локации.

    Доступ только по allowed_ips локации.
    """
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    try:
        verify_desk_access(request, loc_id)
    except HTTPException:
        return templates.TemplateResponse(
            "403.html",
            {
                "request": request,
                "message": f"Доступ к пульта локации '{loc_id}' разрешён только с разрешённых IP."
            },
            status_code=403
        )

    loc = LOCATIONS[loc_id]

    return templates.TemplateResponse(
        "location_dashboard.html",
        {
            "request": request,
            "loc_id": loc_id,
            "loc_name": loc["name"],
            "loc_icon": loc.get("icon", "📍")
        }
    )
    
    
@app.get("/api/v1/desk/{loc_id}/state")
async def get_desk_state(
    loc_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Возвращает состояние одной локации для локального пульта.
    """
    verify_desk_access(request, loc_id, x_api_token)

    loc = LOCATIONS[loc_id]

    state_raw = await redis_client.get(f"dispatch:state:{loc_id}")
    state = json.loads(state_raw) if state_raw else {
        "events": [],
        "updated_at": datetime.now().isoformat()
    }

    return {
        "loc_id": loc_id,
        "name": loc["name"],
        "icon": loc.get("icon", "📍"),
        "mode": get_effective_mode(state["events"]),
        "events": state["events"],
        "updated_at": state["updated_at"],
        "alerts": get_location_alerts(loc_id)
    }
    
@app.get("/api/v1/desk/{loc_id}/templates")
async def get_desk_templates(
    loc_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Возвращает шаблоны оповещений, настроенные для конкретной локации.
    """
    verify_desk_access(request, loc_id, x_api_token)

    alerts = get_location_alerts(loc_id)
    templates_list = []

    for alert in alerts:
        global_template = TEMPLATES.get(alert["template_id"], {})

        templates_list.append({
            "template_id": alert["template_id"],
            "name": alert.get("name", alert["template_id"]),
            "severity": alert.get("severity", "attention"),
            "audio_file": alert.get("audio_file", ""),
            "default_text": global_template.get("default_text", ""),
            "matrix_text": alert.get("text_for_matrix", ""),
            "trigger_code": alert.get("trigger_code", ""),
            "subscribers": alert.get("subscribers", []),
            "subscribers_count": len(alert.get("subscribers", []))
        })

    return {"templates": templates_list}

@app.get("/api/v1/desk/{loc_id}/event-history")
async def get_desk_event_history(
    loc_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Возвращает историю событий диспетчера для одной локации.
    """
    verify_desk_access(request, loc_id, x_api_token)

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    history_key = f"dispatch:history:{loc_id}"
    history_raw = await redis_client.get(history_key)
    history = json.loads(history_raw) if history_raw else []

    return {
        "loc_id": loc_id,
        "loc_name": LOCATIONS[loc_id]["name"],
        "events": history
    }
    
@app.get("/api/v1/desk/{loc_id}/campaign-history")
async def get_desk_campaign_history(
    loc_id: str,
    limit: int = 50,
    template_id: Optional[str] = None,
    request: Request = None,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Возвращает историю голосовых оповещений только для этой локации.
    """
    verify_desk_access(request, loc_id, x_api_token)

    limit = max(1, min(limit, 500))

    history_key = globals().get("CAMPAIGN_HISTORY_KEY", "dispatch:campaign_history")
    history_limit = globals().get("CAMPAIGN_HISTORY_LIMIT", 500)

    raw_items = await redis_client.lrange(history_key, 0, history_limit - 1)

    history = []

    for raw in raw_items:
        try:
            item = json.loads(raw)
        except Exception:
            continue

        if item.get("loc_id") != loc_id:
            continue

        if template_id and item.get("template_id") != template_id:
            continue

        history.append(item)

        if len(history) >= limit:
            break

    return {
        "loc_id": loc_id,
        "history": history,
        "count": len(history),
        "limit": limit
    }
    
@app.post("/api/v1/desk/{loc_id}/alert/trigger")
async def desk_trigger_alert(
    loc_id: str,
    payload: DeskAlertTriggerRequest,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Запуск голосового оповещения из локального пульта локации.

    Доступ только по allowed_ips локации.
    """
    verify_desk_access(request, loc_id, x_api_token)

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    loc_data = LOCATIONS[loc_id]

    template = get_template_by_id(payload.template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    alert_config = next(
        (
            a for a in get_location_alerts(loc_id)
            if a["template_id"] == template["id"]
        ),
        None
    )

    if not alert_config:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template['name']}' not configured for location '{loc_data['name']}'"
        )

    subscribers = alert_config.get("subscribers", [])
    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured for this alert")

    try:
        async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
            voice_payload = {
                "node_id": loc_data.get("asterisk_node", "zv-asterisk"),
                "template_id": template["id"],
                "audio_file": alert_config.get("audio_file", template.get("audio_file", "")),
                "text_for_matrix": alert_config.get("text_for_matrix") or template.get("default_text", ""),
                "subscribers": subscribers,
                "loc_id": loc_id,
                "loc_name": loc_data["name"],
                "initiator": f"local_desk_{loc_id}"
            }

            response = await client.post(
                f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
                json=voice_payload,
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )

            if response.status_code != 200:
                logger.error(f"❌ voice-api returned {response.status_code}: {response.text}")
                raise HTTPException(status_code=502, detail=f"voice-api error: {response.text}")

            result = response.json()
            
            await save_unified_history(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                category="campaign",
                action="triggered",
                severity=template.get("severity", "attention"),
                title=template.get("name", template["id"]),
                description=alert_config.get("text_for_matrix") or template.get("default_text", ""),
                source=f"local_desk_{loc_id}",
                template_id=template["id"],
                template_name=template.get("name", template["id"]),
                campaign_id=result.get("campaign_id"),
                subscribers_count=len(subscribers)
            )
            
            await send_alert_to_matrix(
                loc_id,
                template["id"],
                f"local_desk_{loc_id}"
            )

    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail=f"voice-api unavailable: {str(e)}")

    campaign_id = result.get("campaign_id")

    # Сохраняем историю, если реализована функция из предыдущего шага
    save_history = globals().get("save_campaign_history")
    if campaign_id and save_history:
        await save_history(
            loc_id=loc_id,
            loc_name=loc_data["name"],
            template_id=template["id"],
            template_name=template.get("name", template["id"]),
            severity=template.get("severity", "attention"),
            campaign_id=campaign_id,
            subscribers_count=len(subscribers),
            initiator=f"local_desk_{loc_id}",
            source="local_desk"
        )

    # Отправляем сообщение в Matrix комнату локации
    await send_to_matrix(loc_id, template["id"], f"local_desk_{loc_id}")

    logger.info(f"✅ Desk alert triggered: {template['name']} for {loc_data['name']}")

    return {
        "status": "success",
        "template": template["name"],
        "location": loc_data["name"],
        "subscribers_count": len(subscribers),
        "campaign_id": campaign_id,
        "voice_api_response": result
    }
    
@app.get("/api/v1/desk/{loc_id}/campaign/{campaign_id}/details")
async def get_desk_campaign_details(
    loc_id: str,
    campaign_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Возвращает детали голосовой кампании, но только если она относится к этой локации.
    """
    verify_desk_access(request, loc_id, x_api_token)

    try:
        async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
            response = await client.get(
                f"{settings.VOICE_API_URL}/api/v1/campaign/{campaign_id}/logs",
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )

            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to fetch campaign details")

            data = response.json()

            # Проверяем, что кампания действительно относится к этой локации
            events = data.get("events", [])
            belongs_to_location = any(
                ev.get("loc_id") == loc_id
                for ev in events
            )

            if not belongs_to_location:
                raise HTTPException(
                    status_code=403,
                    detail="Campaign does not belong to this location"
                )

            return data

    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail="voice-api unavailable")
    
@app.get("/api/v1/desk/{loc_id}/history")
async def get_desk_unified_history(
    loc_id: str,
    limit: int = 100,
    category: Optional[str] = None,
    action: Optional[str] = None,
    template_id: Optional[str] = None,
    request: Request = None,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    """
    Единая история для локального пульта конкретной локации.
    """
    verify_desk_access(request, loc_id, x_api_token)

    limit = max(1, min(limit, 500))

    loc_key = UNIFIED_HISTORY_LOC_KEY_TEMPLATE.format(loc_id=loc_id)

    raw_items = await redis_client.lrange(
        loc_key,
        0,
        UNIFIED_HISTORY_LIMIT - 1
    )

    history = []

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

        history.append(item)

        if len(history) >= limit:
            break

    return {
        "loc_id": loc_id,
        "history": history,
        "count": len(history),
        "limit": limit
    }

    
@app.get("/api/v1/history")
async def get_unified_history(
    limit: int = 100,
    loc_id: Optional[str] = None,
    category: Optional[str] = None,
    action: Optional[str] = None,
    template_id: Optional[str] = None,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """
    Единая история дашборда.

    Фильтры:
    - loc_id
    - category: incident / campaign / system
    - action: created / closed / cleared / auto_closed / triggered
    - template_id
    """
    from app.core.security import verify_ip

    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)

    limit = max(1, min(limit, 500))

    raw_items = await redis_client.lrange(
        UNIFIED_HISTORY_GLOBAL_KEY,
        0,
        UNIFIED_HISTORY_LIMIT - 1
    )

    history = []

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

        history.append(item)

        if len(history) >= limit:
            break

    return {
        "history": history,
        "count": len(history),
        "limit": limit
    }