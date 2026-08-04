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
from app.security import (
    verify_dashboard_access,
    verify_location_access_or_dashboard,
    get_matrix_user_read,
    get_matrix_user_write,
    get_location_events,
    get_template_by_id,
    MatrixUser,
    LOCATIONS,
    CONFIG
)
from app.models import (
    EventCreate,
    EventClose,
    AsteriskTriggerRequest, 
    VoiceTriggerRequest,
    AlertTriggerRequest
)

from app.alert_svc import ALERT_CONFIG, resolve_subscribers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Matrix Incident Dispatcher")
templates = Jinja2Templates(directory="templates")

redis_client = None

@app.on_event("startup")
async def startup_event():
    global redis_client
    redis_client = redis.from_url(CONFIG.get("redis_url", "redis://redis:6379/4"), decode_responses=True)
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
        
        result[loc_id] = {
            "name": loc_data["name"],
            "icon": loc_data.get("icon", "📍"),
            "mode": get_effective_mode(state["events"]),
            "events": state["events"],
            "updated_at": state["updated_at"]
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
    if event_type == "attention" and CONFIG.get("auto_close_attention", 0) > 0:
        ttl_minutes = CONFIG["auto_close_attention"]
    elif event_type == "alarm" and CONFIG.get("auto_close_alarm", 0) > 0:
        ttl_minutes = CONFIG["auto_close_alarm"]
    
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
    
    await send_to_matrix(loc_id, event_type, comment, user.user_id)
    
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
    
    if not state["events"]:
        await send_to_matrix(loc_id, "normal", "Все инциденты закрыты", "system")
    
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
    await send_to_matrix(loc_id, "normal", "Все инциденты закрыты принудительно", "system")
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

async def send_to_matrix(loc_id: str, event_type: str, comment: str, author_user_id: str):
    loc = LOCATIONS.get(loc_id, {})
    mode_names = {"normal": "НОРМА", "attention": "ВНИМАНИЕ", "alarm": "ТРЕВОГА"}
    mode_name = mode_names.get(event_type, event_type.upper())
    
    tpl = loc.get("templates", {}).get(f"widget_{event_type}", 
             f"{'🚨' if event_type == 'alarm' else ('✅' if event_type == 'normal' else '⚠️')} **{loc.get('name', loc_id)}: {mode_name}!**\nИнициатор: {author_user_id or 'system'}\nКомментарий: {comment}\nВремя: {{time}}")
    
    tz = ZoneInfo(CONFIG.get("timezone", "Asia/Novosibirsk"))
    time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")
    message = tpl.replace("{time}", time_str)
    
    html_body = markdown.markdown(message, extensions=['nl2br'])
    payload = {
        "msgtype": "m.text",
        "body": message,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body
    }
    
    txn_id = f"widget_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:4]}"
    url = f"{CONFIG['hs']}/_matrix/client/v3/rooms/{loc['room_id']}/send/m.room.message/{txn_id}"
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.put(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {CONFIG['bot_token']}"}
            )
            if resp.status_code not in (200, 201):
                print(f"[MATRIX ERROR] {resp.text}")
        except Exception as e:
            print(f"[MATRIX EXCEPTION] {e}")

# --- История и автозакрытие ---

async def save_to_history(loc_id: str, event: dict, close_reason: str):
    history_key = f"dispatch:history:{loc_id}"
    event["closed_at"] = datetime.now().isoformat()
    event["close_reason"] = close_reason
    
    history_raw = await redis_client.get(history_key)
    history = json.loads(history_raw) if history_raw else []
    history.insert(0, event)
    history = history[:100]
    
    ttl_seconds = CONFIG.get("history_ttl_hours", 48) * 3600
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
            "dashboard_token": CONFIG.get("dashboard_token","")
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
    expected_token = CONFIG.get("asterisk_api_token", "default_secret_token")
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

@app.post("/api/v1/incident/voice-trigger")
async def trigger_from_voice_api(
    request_data: VoiceTriggerRequest,
    x_api_token: str = Header(..., alias="X-API-Token")
):
    """Принимает транскрибированный голос от voice-api и маршрутизирует по комнатам"""
    
    # 1. Проверка внутреннего токена (защита от внешних вызовов)
    expected_token = CONFIG.get("internal_api_token", "super_secret_internal_token_123")
    if x_api_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid internal token")

    # 2. Ищем локацию по номеру exten (например, "801")
    target_loc_id = None
    target_loc_data = None
    for loc_id, loc_data in LOCATIONS.items():
        if str(loc_data.get("asterisk_exten")) == request_data.exten:
            target_loc_id = loc_id
            target_loc_data = loc_data
            break

    if not target_loc_id:
        raise HTTPException(status_code=404, detail=f"Локация с exten '{request_data.exten}' не найдена в config/locations.json")

    # 3. Создаем событие
    event_id = str(uuid.uuid4())[:8]
    new_event = {
        "id": event_id,
        "type": "alarm", 
        "comment": f"🎙️ [Голос от {request_data.caller_id}]: {request_data.comment}",
        "timestamp": datetime.now().isoformat(),
        "source": f"asterisk_exten_{request_data.exten}"
    }
    
    # 4. Сохраняем в Redis
    key = f"dispatch:state:{target_loc_id}"
    state_raw = await redis_client.get(key)
    state = json.loads(state_raw) if state_raw else {"events": [], "updated_at": ""}
    
    state["events"].append(new_event)
    state["updated_at"] = datetime.now().isoformat()
    await redis_client.set(key, json.dumps(state, ensure_ascii=False))
    
    # 5. Отправляем в Matrix через нашу проверенную функцию send_to_matrix
    await send_to_matrix(target_loc_id, "alarm", new_event["comment"], "Asterisk Voice Bot")
    
    logger.info(f"✅ Voice incident routed to {target_loc_data['name']} ({target_loc_id}), event_id: {event_id}")
    return {"status": "success", "event_id": event_id, "location": target_loc_data["name"]}




@app.post("/api/v1/alert/trigger")
async def trigger_alert_template(
    request_data: AlertTriggerRequest,
    user: MatrixUser = Depends(get_matrix_user_write)
):
    """Ручной запуск шаблона голосового оповещения"""
    
    # 1. Находим локацию
    loc_id = request_data.loc_id
    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found")
    
    loc_data = LOCATIONS[loc_id]
    
    # 2. Находим шаблон
    template = get_template_by_id(request_data.template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not found")
    
    # 3. Находим событие для этой локации и шаблона
    location_events = get_location_events(loc_id)
    event_config = None
    for event in location_events:
        if event["template_id"] == template["id"]:
            event_config = event
            break
    
    if not event_config:
        raise HTTPException(
            status_code=404, 
            detail=f"Template '{template['name']}' not configured for location '{loc_data['name']}'"
        )
    
    # 4. Формируем запрос к voice-api
    node_id = loc_data.get("asterisk_node", "hq_main")
    subscribers = event_config.get("subscribers", [])
    
    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured for this alert")
    
    # 5. Отправляем в voice-api
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "node_id": node_id,
                "template_id": template["id"],
                "audio_file": template["audio_file"],
                "text_for_matrix": template["text_for_matrix"],
                "subscribers": subscribers,
                "loc_id": loc_id,
                "loc_name": loc_data["name"],
                "initiator": user.user_id
            }
            
            # URL voice-api (настраивается через env)
            voice_api_url = CONFIG.get("voice_api_url", "http://voice-api:8000")
            voice_api_secret = CONFIG.get("voice_api_secret", "")
            
            response = await client.post(
                f"{voice_api_url}/api/v1/campaign/trigger-template",
                json=payload,
                headers={"X-Secret": voice_api_secret}
            )
            
            if response.status_code != 200:
                logger.error(f"❌ voice-api returned {response.status_code}: {response.text}")
                raise HTTPException(
                    status_code=502, 
                    detail=f"voice-api error: {response.text}"
                )
            
            result = response.json()
            
    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail="voice-api unavailable")
    
    # 6. Отправляем текст в Matrix (параллельно с обзвоном)
    await send_to_matrix(loc_id, template["severity"], template["text_for_matrix"], user.user_id)
    
    logger.info(f"✅ Alert triggered: {template['name']} for {loc_data['name']}")
    
    return {
        "status": "success",
        "template": template["name"],
        "location": loc_data["name"],
        "subscribers_count": len(subscribers),
        "voice_api_response": result
    }
    

@app.post("/api/v1/alert/trigger")
async def trigger_alert_template(
    request_data: AlertTriggerRequest,
    user: MatrixUser = Depends(get_matrix_user_write) # Твоя существующая зависимость
):
    """Ручной запуск шаблона голосового оповещения из UI диспетчера"""
    
    loc_id = request_data.loc_id
    loc_config = ALERT_CONFIG.get("locations", {}).get(loc_id)
    if not loc_config:
        raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found in templates")
    
    template = get_template_by_id(request_data.template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{request_data.template_id}' not found")
    
    # Находим конфигурацию события для этой локации (или берем дефолтную)
    event_config = next((ev for ev in loc_config.get("events", []) if ev["template_id"] == template["id"]), {})
    
    subscribers = resolve_subscribers(loc_config, event_config)
    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured for this alert")
    
    # 1. Отправляем запрос в voice-api
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "node_id": "zv-astreisk", # Пока жестко, позже можно вынести в конфиг локации
                "template_id": template["id"],
                "audio_file": template["audio_file"],
                "text_for_matrix": template["text_for_matrix"],
                "subscribers": subscribers,
                "loc_id": loc_id,
                "loc_name": loc_config["name"],
                "initiator": user.user_id
            }
            
            voice_api_url = CONFIG.get("voice_api_url", "http://voice-api:8000")
            voice_api_secret = CONFIG.get("voice_api_secret", "")
            
            response = await client.post(
                f"{voice_api_url}/api/v1/campaign/trigger-template",
                json=payload,
                headers={"X-Secret": voice_api_secret}
            )
            
            if response.status_code != 200:
                logger.error(f"❌ voice-api returned {response.status_code}: {response.text}")
                raise HTTPException(status_code=502, detail=f"voice-api error: {response.text}")
                
            voice_result = response.json()
            
    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise HTTPException(status_code=503, detail="voice-api unavailable")
    
    # 2. Параллельно отправляем текст в Matrix (используй свою существующую функцию отправки)
    # await send_to_matrix(loc_config["matrix_room_id"], template["severity"], template["text_for_matrix"], user.user_id)
    logger.info(f"📨 Matrix message would be sent to {loc_config['matrix_room_id']}")
    
    logger.info(f"✅ Alert triggered: {template['name']} for {loc_config['name']}")
    
    return {
        "status": "success",
        "template": template["name"],
        "location": loc_config["name"],
        "subscribers_count": len(subscribers),
        "voice_api_response": voice_result
    }


@app.exception_handler(404)
async def custom_404(request: Request, exc):
    return templates.TemplateResponse(
        "404.html",
        {"request": request, "message": "Запрашиваемый ресурс не найден"},
        status_code=404
    )