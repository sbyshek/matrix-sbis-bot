from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import redis.asyncio as redis
import json
import uuid
import asyncio
from datetime import datetime, timedelta
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
    LOCATIONS
    
)
from app.models import (
    EventCreate,
    EventClose,
    AsteriskTriggerRequest, 
    VoiceTriggerRequest,
    AlertTriggerRequest
)

from app.alert_svc import ALERT_CONFIG, resolve_subscribers, ALERT_TEMPLATES_FILE
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Matrix Incident Dispatcher")
templates = Jinja2Templates(directory="templates")

redis_client = None

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



# --- API ENDPOINTS ---

@app.get("/api/dashboard")
async def get_dashboard_state(
    request: Request,
    _=Depends(verify_dashboard_access)
):
    result = {}
    for loc_id, loc_data in LOCATIONS.items():
        state_raw = await redis_client.get(f"dispatch:state:{loc_id}")
        state = json.loads(state_raw) if state_raw else {"events": [], "updated_at": datetime.now().isoformat()}
        
        # logger.info(f"State for {loc_id}: {state}")
        
        #  Собираем сценарии для этой локации из ALERT_CONFIG
        location_events = ALERT_CONFIG.get("location_events", {}).get(loc_id, [])
        # logger.info(f"Location events for {loc_id}: {location_events}")
        alerts = []
        for ev in location_events:
            # logger.info(f"Events on location {loc_id}: {ev}")
            template = get_template_by_id(ev["template_id"])
            if template:
                alerts.append({
                    "template_id": template["id"],
                    "name": template["name"],
                    "severity": template["severity"],
                    "text_for_matrix": template.get("text_for_matrix", ""),
                    "trigger_code": ev.get("trigger_code", ""),
                    "subscribers": ev.get("subscribers", [])
                })
        
        result[loc_id] = {
            "name": loc_data["name"],
            "icon": loc_data.get("icon", "📍"),
            "mode": get_effective_mode(state["events"]),
            "events": state["events"],
            "updated_at": state["updated_at"],
            "alerts": alerts  # 🔥 НОВОЕ ПОЛЕ
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
    
    # await send_to_matrix(loc_id, event_type, comment, user.user_id)
    
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
    await save_to_history(loc_id, event_to_close, "manual")
    await redis_client.delete(f"dispatch:ttl:{loc_id}:{event_id}")
    
    # if not state["events"]:
        # await send_to_matrix(loc_id, "normal", "Все инциденты закрыты", "system")
    
    state["updated_at"] = datetime.now().isoformat()
    await redis_client.set(key, json.dumps(state, ensure_ascii=False))
    
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
    # await send_to_matrix(loc_id, "normal", "Все инциденты закрыты принудительно", "system")
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

async def send_to_matrix(loc_id: str, template_id: str, author_user_id: str):
    """Отправляет сообщение в Matrix на основе единого конфига"""
    loc_data = LOCATIONS.get(loc_id)
    if not loc_data or "room_id" not in loc_data:
        logger.error(f" Matrix: Локация {loc_id} не найдена или нет room_id")
        return

    # Ищем шаблон внутри локации
    alert_template = None
    for alert in loc_data.get("alerts", []):
        if alert["template_id"] == template_id:
            alert_template = alert
            break

    if not alert_template:
        logger.error(f"❌ Matrix: Шаблон {template_id} не найден для локации {loc_id}")
        return

    # Формируем текст
    tz = ZoneInfo(settings.TIMEZONE)
    time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")
    
    # Подставляем переменные в шаблон
    message = alert_template["matrix_text"].replace("{time}", time_str)
    message = message.replace("{author}", author_user_id or "система")
    
    html_body = markdown.markdown(message, extensions=['nl2br'])
    
    payload = {
        "msgtype": "m.text",
        "body": message,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body
    }
    
    txn_id = f"alert_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:4]}"
    url = f"{settings.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{loc_data['room_id']}/send/m.room.message/{txn_id}"
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.put(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {settings.MATRIX_ACCESS_TOKEN}"}
            )
            if resp.status_code in (200, 201):
                logger.info(f"✅ Matrix message sent to {loc_data['name']}")
            else:
                logger.error(f"❌ Matrix API error {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.error(f"❌ Matrix connection error: {e}")


# async def send_to_matrix(loc_id: str, event_type: str, comment: str, author_user_id: str):
#     loc = LOCATIONS.get(loc_id, {})
#     mode_names = {"normal": "НОРМА", "attention": "ВНИМАНИЕ", "alarm": "ТРЕВОГА"}
#     mode_name = mode_names.get(event_type, event_type.upper())
    
#     tpl = loc.get("templates", {}).get(f"widget_{event_type}", 
#              f"{'🚨' if event_type == 'alarm' else ('✅' if event_type == 'normal' else '⚠️')} **{loc.get('name', loc_id)}: {mode_name}!**\nИнициатор: {author_user_id or 'system'}\nКомментарий: {comment}\nВремя: {{time}}")
    
#     tz = ZoneInfo(settings.TIMEZONE)
#     time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")
#     message = tpl.replace("{time}", time_str)
    
#     html_body = markdown.markdown(message, extensions=['nl2br'])
#     payload = {
#         "msgtype": "m.text",
#         "body": message,
#         "format": "org.matrix.custom.html",
#         "formatted_body": html_body
#     }
    
#     txn_id = f"widget_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:4]}"
#     url = f"{settings.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{loc['room_id']}/send/m.room.message/{txn_id}"
    
#     async with httpx.AsyncClient() as client:
#         try:
#             resp = await client.put(
#                 url,
#                 json=payload,
#                 headers={"Authorization": f"Bearer {settings.MATRIX_ACCESS_TOKEN}"}
#             )
#             if resp.status_code not in (200, 201):
#                 print(f"[MATRIX ERROR] {resp.text}")
#         except Exception as e:
#             print(f"[MATRIX EXCEPTION] {e}")

# --- История и автозакрытие ---

async def save_to_history(loc_id: str, event: dict, close_reason: str):
    history_key = f"dispatch:history:{loc_id}"
    event["closed_at"] = datetime.now().isoformat()
    event["close_reason"] = close_reason
    
    history_raw = await redis_client.get(history_key)
    history = json.loads(history_raw) if history_raw else []
    history.insert(0, event)
    history = history[:100]
    
    ttl_seconds = settings.HISTORY_TTL_HOURS * 3600
    await redis_client.set(history_key, json.dumps(history, ensure_ascii=False), ex=ttl_seconds)

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
                                await save_to_history(loc_id, event_to_close, "auto")
                                await send_to_matrix(loc_id, "normal", f"Автозакрытие: {event_to_close['comment']}", "system")
                                state["updated_at"] = datetime.now().isoformat()
                                await redis_client.set(key, json.dumps(state, ensure_ascii=False))
                                print(f"[AUTO-CLOSE] Закрыто событие {event_id} в {loc_id}")
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



@app.post("/api/v1/alert/trigger")
async def trigger_alert_template(
    request_data: AlertTriggerRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    """Ручной запуск шаблона голосового оповещения из UI диспетчера"""
    from app.core.security import verify_ip
    
    # 1. Проверка безопасности: принимаем либо X-Local-Token, либо X-API-Token
    token = x_local_token or x_api_token
    if not token:
        raise HTTPException(status_code=403, detail="Требуется токен авторизации")
    
    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Неверный токен")
        
    verify_ip(request)
    
    # 2. Находим локацию
    loc_id = request_data.loc_id
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found")
    loc_data = LOCATIONS[loc_id]
    
    # 3. Находим шаблон
    template = get_template_by_id(request_data.template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not found")
    
    # 4. Находим событие для этой локации
    location_events = get_location_events(loc_id)
    event_config = next((ev for ev in location_events if ev["template_id"] == template["id"]), None)
    
    if not event_config:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template['name']}' not configured for location '{loc_data['name']}'"
        )
    
    # 5. Формируем список абонентов
    subscribers = event_config.get("subscribers", [])
    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured for this alert")
    
    # 6. Отправляем в voice-api
    try:
        async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
            payload = {
                "node_id": loc_data.get("asterisk_node", "zv-asterisk"),
                "template_id": template["id"],
                "audio_file": template["audio_file"],
                "text_for_matrix": template["text_for_matrix"],
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

@app.post("/api/v1/alert/trigger-by-code")
async def trigger_alert_by_code(
    request_data: AlertTriggerByCodeRequest,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    request: Request = None
):
    """Запуск оповещения по коду с телефона (IVR)"""
    verify_local_token(x_local_token)
    verify_ip(request)
    
    # Ищем шаблон по коду во всех локациях
    template = None
    loc_config = None
    loc_id = None
    
    for lid, events in ALERT_CONFIG.get("location_events", {}).items():
        for event in events:
            if event.get("trigger_code") == request_data.code:
                template = get_template_by_id(event["template_id"])
                loc_id = lid
                loc_config = LOCATIONS.get(lid)
                subscribers = event.get("subscribers", [])
                break
        if template:
            break
    
    if not template or not loc_config:
        raise HTTPException(status_code=404, detail=f"Alert code '{request_data.code}' not found")
    
    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured")
    
    # Отправляем в voice-api
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "node_id": loc_config.get("asterisk_node", "zv-astreisk"),
                "template_id": template["id"],
                "audio_file": template["audio_file"],
                "text_for_matrix": template["text_for_matrix"],
                "subscribers": subscribers,
                "loc_id": loc_id,
                "loc_name": loc_config["name"],
                "initiator": f"ivr_{request_data.caller}"
            }
            
            response = await client.post(
                f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
                json=payload,
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="voice-api error")
            
            voice_result = response.json()
            
    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail="voice-api unavailable")
    
    # Отправляем в Matrix
    await send_to_matrix(loc_id, template["severity"], template["text_for_matrix"], f"IVR: {request_data.caller}")
    
    logger.info(f"✅ IVR Alert triggered: {template['name']} for {loc_config['name']} by {request_data.caller}")
    
    return {
        "status": "success",
        "template": template["name"],
        "location": loc_config["name"],
        "campaign_id": voice_result.get("campaign_id"),
        "triggered_by": request_data.caller
    }


# =============================================================================
# Получение деталей кампании из voice-api (для UI)
# =============================================================================
@app.get("/api/v1/campaign/{campaign_id}/details")
async def get_campaign_details(
    campaign_id: str,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    request: Request = None
):
    """Получает детали кампании из voice-api"""
    verify_local_token(x_local_token)
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
# Список локаций с доступными шаблонами (для UI)
# =============================================================================
@app.get("/api/v1/locations")
async def get_locations(
    x_local_token: str = Header(..., alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Возвращает список локаций с доступными шаблонами оповещений"""
    
    verify_ui_access(request, x_local_token, x_api_token)
    
    # token = x_local_token or x_api_token
    # if not token:
    #     raise HTTPException(status_code=403, detail="Token required")
    # verify_local_token(token)
    # verify_ip(request)
        
    
    locations = []
    for loc_id, loc_config in LOCATIONS.items():
        alerts = []
        location_events = ALERT_CONFIG.get("location_events", {}).get(loc_id, [])
        
        for event in location_events:
            template = get_template_by_id(event["template_id"])
            if template:
                alerts.append({
                    "template_id": template["id"],
                    "name": template["name"],
                    "severity": template["severity"],
                    "trigger_code": event.get("trigger_code")
                })
        
        locations.append({
            "id": loc_id,
            "name": loc_config["name"],
            "icon": loc_config.get("icon", "📍"),
            "alerts": alerts
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
@app.get("/api/v1/templates")
async def get_templates(
    x_local_token: str = Header(..., alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Возвращает список всех шаблонов оповещений"""
    # verify_local_token(x_local_token)
    # verify_ip(request)
    
    verify_ui_access(request, x_local_token, x_api_token)
    
    return {
        "templates": ALERT_CONFIG.get("audio_templates", [])
    }

@app.post("/api/v1/templates")
async def create_or_update_template(
    template_data: dict,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Создаёт или обновляет шаблон оповещения"""
    # verify_local_token(x_local_token)
    # verify_ip(request)
    verify_ui_access(request, x_local_token, x_api_token)
    
    template_id = template_data.get("id")
    if not template_id:
        raise HTTPException(status_code=400, detail="Template ID is required")
    
    # Проверяем, существует ли шаблон
    templates = ALERT_CONFIG.get("audio_templates", [])
    existing_idx = next((i for i, t in enumerate(templates) if t["id"] == template_id), None)
    
    if existing_idx is not None:
        # Обновление
        templates[existing_idx] = template_data
        logger.info(f"✏️ Template updated: {template_id}")
    else:
        # Создание
        templates.append(template_data)
        logger.info(f" Template created: {template_id}")
    
    # Сохраняем в файл
    try:
        with open(ALERT_TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(ALERT_CONFIG, f, ensure_ascii=False, indent=2)
        return {"status": "success", "template_id": template_id}
    except Exception as e:
        logger.error(f" Failed to save templates: {e}")
        raise HTTPException(status_code=500, detail="Failed to save template")

@app.delete("/api/v1/templates/{template_id}")
async def delete_template(
    template_id: str,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    """Удаляет шаблон оповещения"""
    # verify_local_token(x_local_token)
    # verify_ip(request)
    verify_ui_access(request, x_local_token, x_api_token)
    
    templates = ALERT_CONFIG.get("audio_templates", [])
    templates = [t for t in templates if t["id"] != template_id]
    ALERT_CONFIG["audio_templates"] = templates
    
    try:
        with open(ALERT_TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(ALERT_CONFIG, f, ensure_ascii=False, indent=2)
        logger.info(f"🗑️ Template deleted: {template_id}")
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