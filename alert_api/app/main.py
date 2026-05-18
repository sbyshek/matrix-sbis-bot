import os
import json
import asyncio
import time
import re
import httpx
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
import redis
from fastapi import FastAPI, Depends, HTTPException, Header, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

templates = Jinja2Templates(directory="/app/templates")


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Alert Bus API v3 (Centralized)", version="3.0")
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    # password=os.getenv("REDIS_PASSWORD"),
    decode_responses=True,
    socket_timeout=3, socket_connect_timeout=2
)

CONFIG_FILE = "/app/config/sources.json"
LOG_DIR = "/app/logs"
MAX_LOG_AGE_DAYS = 30
DRIVERS_FILE = "/app/config/drivers.json"

os.makedirs(LOG_DIR, exist_ok=True)

# Кэши в памяти
_sources: List[Dict] = []
_token_map: Dict[str, str] = {}  # token -> source
_route_map: Dict[str, str] = {}  # source -> room

def load_config():
    global _sources, _token_map, _route_map
    if not os.path.exists(CONFIG_FILE):
        logger.warning("⚠️ sources.json not found. Creating empty.")
        _sources, _token_map, _route_map = [], {}, {}
        return
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _sources = json.load(f)
    _token_map = {s["token"]: s["source"] for s in _sources if s.get("active")}
    _route_map = {s["source"]: s["room"] for s in _sources}
    logger.info(f"📂 Config loaded: {len(_token_map)} tokens, {len(_route_map)} routes")

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_sources, f, indent=2, ensure_ascii=False)
        
        
def normalize_number(val: str) -> Optional[float]:
        if not val:
            return None
        try:
            # Заменяем запятую на точку и пробуем распарсить
            return float(str(val).replace(",", "."))
        except:
            return None

def get_drivers_map() -> dict:
    try:
        with open(DRIVERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return {}


class DriverAlertInput(BaseModel):
    user_id: str
    vehicle_id: str
    alert_type: str
    label: str
    comment: str = ""



load_config()

# 🔐 Авторизация внешних систем
async def verify_client_token(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    source = _token_map.get(token)
    if not source:
        raise HTTPException(403, "Invalid or revoked token")
    return source

# 🤖 Авторизация бота
async def verify_bot_token(x_bot_token: str = Header(None)) -> str:
    expected = os.getenv("BOT_TOKEN")
    if not x_bot_token or x_bot_token != expected:
        masked = x_bot_token[:8] + "****" if x_bot_token else "None"
        logger.warning(f"⚠️ Bot auth failed. Got: {masked}, Expected: {expected[:8]}****")
        raise HTTPException(403, "Bot auth failed")
    return "ok"

# 👑 Авторизация админа
async def verify_admin_key(admin_key: str = Header(None)) -> str:
    expected = os.getenv("ADMIN_KEY")
    if not admin_key or admin_key != expected:
        masked = admin_key[:8] + "****" if admin_key else "None"
        logger.warning(f"⚠️ Admin-key auth failed. Got: {masked}, Expected: {expected[:8]}****")
        raise HTTPException(403, "Admin auth failed")
    
    

class Alert(BaseModel):
    location: Optional[str] = None
    description: Optional[str] = None
    parameter: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    severity: str = "info"
    meta: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None

# === ENDPOINTS ===

@app.post("/api/v1/ingest", status_code=201)
async def ingest_alert(
    alert: Alert,
    verified_source: str = Depends(verify_client_token)
):
    data = alert.model_dump()
    data["source"] = verified_source
    data["timestamp"] = datetime.now().isoformat()

    redis_client.lpush("bus:all", json.dumps(data))
    redis_client.ltrim("bus:all", 0, 1999)

    with open(os.path.join(LOG_DIR, "alerts_active.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")

    logger.info(f"✅ [{alert.severity}] {verified_source}")
    return {"status": "ok", "source": verified_source}





@app.get("/web/success", response_class=HTMLResponse)
async def success_page(request: Request):
    return templates.TemplateResponse(request,
        name="success.html", 
        context={
        "request": request,
        "message": "Алерт успешно отправлен!"
    })

@app.get("/api/v1/stream/all")
async def get_all_stream(
    since: str = None, 
    limit: int = 50,
    _: str = Depends(verify_bot_token)  # 🔐 Только авторизованный бот
):
    """Опрос очереди событий. Возвращает события новее 'since'."""
    limit = min(limit, 200)  # Защита от чрезмерной нагрузки
    
    # 🔑 Используем тот же ключ, что и в ingest: "bus:all"
    items = redis_client.lrange("bus:all", 0, -1)
    
    events = []
    for item in items:
        try:
            evt = json.loads(item)
            if since is None or evt["timestamp"] > since:
                events.append(evt)
        except json.JSONDecodeError:
            continue
            
    # Возвращаем от старых к новым (чтобы бот отправлял в правильном порядке)
    return list(reversed(events[:limit]))

@app.get("/api/v1/config/routes")
async def get_routes_for_bot(_: str = Depends(verify_bot_token)):
    # """Возвращает маппинг source -> room только для авторизованного бота"""
    # return _route_map
    """Возвращает расширенную информацию об источниках для бота"""
    # Формат: source → {room, comment, severity_default}
    return {
        s["source"]: {
            "room": s["room"],
            "comment": s.get("comment", s["source"]),  # Fallback на source если нет comment
            "severity": s.get("severity", "info")
        }
        for s in _sources if s.get("active")
    }

@app.get("/api/v1/admin/sources")
async def list_sources(_: str = Depends(verify_admin_key)):
    return _sources

@app.post("/api/v1/admin/sources")
async def add_source(source: dict, _: str = Depends(verify_admin_key)):
    required = ["source", "token", "room"]
    if not all(k in source for k in required):
        raise HTTPException(400, "Missing fields: source, token, room")
    _sources.append({**source, "active": True})
    save_config()
    load_config()
    return {"status": "added", "source": source["source"]}


@app.post("/api/v1/admin/reload")
async def reload_config(_: str = Depends(verify_admin_key)):
    """Перечитывает sources.json в память без рестарта контейнера"""
    try:
        load_config()
        return {
            "status": "ok", 
            "message": "config reloaded", 
            "routes": len(_route_map), 
            "tokens": len(_token_map)
        }
    except Exception as e:
        logger.error(f"Config reload failed: {e}")
        raise HTTPException(500, f"Reload error: {str(e)}")

@app.get("/api/v1/history")
async def get_history(
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,
    _: str = Depends(verify_admin_key)
):
    """
    Чтение истории из JSONL-лога.
    - limit: кол-во записей (max 200)
    - offset: сдвиг от конца (0=последние, 50=предыдущие 50)
    - source: фильтр по источнику (опционально)
    """
    log_path = CONFIG_FILE
    if not os.path.exists(log_path):
        return []
    
    limit = min(limit, 200)  # Защита от чтения слишком большого куска
    
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # Читаем с конца файла
    total = len(lines)
    start_idx = max(0, total - offset - limit)
    end_idx = max(0, total - offset)
    
    events = []
    for line in lines[start_idx:end_idx]:
        try:
            evt = json.loads(line.strip())
            if source is None or evt.get("source") == source:
                events.append(evt)
        except json.JSONDecodeError:
            continue
    
    return {
        "total_lines": total,
        "returned": len(events),
        "offset": offset,
        "limit": limit,
        "events": list(reversed(events))  # Возвращаем от новых к старым
    }

@app.get("/web/alert-form")  # ← response_class НУЖЕН для HTML
async def show_alert_form(
    request: Request,
    source: str = "Unknown",
    location: str = "",
    token: str = "",
    parameter: str = "",
    norm: str = "",
    value: str = "",
    unit: str = "",
    comment: str = "",
    meta: str = "",
    context: str = "",
    severity: str = "info"  
    
):
    # Парсинг нормы
    min_val, max_val = 0.0, 0.0
    try:
        # if "-" in norm:
        #     parts = norm.split("-")
        #     min_val = float(parts[0].strip())
        #     max_val = float(parts[1].strip())
        if norm:
        # Нормализуем: заменяем запятые на точки (для дробных)
            norm_clean = norm.replace(",", ".")
            
            # Regex ищет паттерн: число (возможно с минусом) + разделитель + число
            match = re.search(r'([-+]?\d*\.?\d+)\s*[-/]\s*([-+]?\d*\.?\d+)', norm_clean)
            if match:
                try:
                    min_val = float(match.group(1))
                    max_val = float(match.group(2))
                    logger.debug(f"✅ Parsed norm '{norm}' → min={min_val}, max={max_val}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to convert norm to float: {e}")
    except: pass

    # Парсинг meta
    meta_dict = {}
    if meta:
        try:
            import json
            meta_dict = json.loads(meta)
        except Exception as e:
            logger.warning(f"Failed to parse meta JSON: {e}")
    
    # Парсинг context
    context_dict = {}
    if context:
        try:
            import json
            context_dict = json.loads(context)
        except Exception as e:
            logger.warning(f"Failed to parse context JSON: {e}")
    
    # Определение severity
    severity = "info"
    try:
        v = float(value)
        if v > max_val or v < min_val:
            severity = "warning"
    except: pass



    # Рендеринг шаблона (именованные аргументы!)
    return templates.TemplateResponse(
        request,
        name="alert_form.html",
        context={
            "request": request,  # ← Обязательно!
            "source": source,
            "token": token,
            "source_name": source.replace("_", " ").upper(),
            "location": location or "Производство",
            "parameter": parameter,
            "norm_text": norm,
            "min_val": min_val,
            "max_val": max_val,
            "value": value,
            "unit": unit,
            "description": comment,
            "severity": severity,
            "meta": meta_dict,
            "context": context_dict
        }
    )



@app.post("/web/submit")
async def submit_web_alert(
    request: Request,
    token: str = Form(...),
    location: str = Form(None),
    parameter: str = Form(None),
    value: str = Form(None),
    min_val: str = Form(None),
    max_val: str = Form(None),
    unit: str = Form(None),
    description: str = Form(None),
    severity: str = Form("info"),
    meta: str = Form(None),
    context: str = Form(None)
):
    # 1️ Валидация токена на уровне формы
    if token not in _token_map :
        raise HTTPException(401, "Invalid token")
    
    meta_dict = None
    if meta:
        try:
            import json
            meta_dict = json.loads(meta)
        except:
            logger.warning("Failed to parse meta JSON")

    context_dict = None
    if context and context.strip():  # ← ПРОВЕРКА: не пустая ли строка
        try:
            import json
            context_dict = json.loads(context)
            logger.info(f"✅ Parsed context: {context_dict}")
        except Exception as e:
            logger.error(f"❌ Failed to parse context JSON: {e}")
            logger.error(f"   Raw context value: '{context}'")  # Покажет, что пришло
    else:
        logger.warning("⚠️ Context field is empty or None")

    # 2️ Формируем чистый JSON-пакет (как ожидают внешние системы)
    payload = {
        "location": location,
        "parameter": parameter,
        "value": normalize_number(value) if value else None,
        "unit": unit if unit else None,
        "min_val": normalize_number(min_val) if min_val else None,
        "max_val": normalize_number(max_val) if max_val else None,
        "description": description,
        "severity": severity,
        "meta": meta_dict,
        "context": context_dict
    }

    # 3️ Проксируем на внутренний API с правильным заголовком
    internal_url = "http://127.0.0.1:8000/api/v1/ingest"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(internal_url, json=payload, headers=headers, timeout=5.0)

    # 4️⃣ Редирект на страницу успеха или ошибка
    if resp.status_code == 201:
        return RedirectResponse(url="/web/success", status_code=303)
    else:
        logger.error(f"API proxy failed: {resp.status_code} {resp.text}")
        raise HTTPException(500, f"Failed to send alert to API: {resp.status_code}")


# === 🚛 DRIVER WIDGET ENDPOINTS ===


class DriverAlertInput(BaseModel):
    user_id: str
    vehicle_id: str
    alert_type: str
    label: str
    comment: str = ""
    vehicle_number: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

@app.get("/driver/check_access")
async def check_driver_access(user_id: str, vehicle_id: str):
    """Проверяет, есть ли юзер в списке водителей для этого автобуса"""
    drivers = get_drivers_map()
    allowed = drivers.get(vehicle_id, [])
    return {"allowed": user_id in allowed, "vehicle_id": vehicle_id}

@app.post("/driver/alert")
async def receive_driver_alert(
    alert: DriverAlertInput,
    verified_source: str = Depends(verify_client_token)
):
    # 1. 🔒 Проверка прав
    if alert.user_id not in get_drivers_map().get(alert.vehicle_id, []):
        raise HTTPException(403, "Driver not authorized")

    # 2. 📦 Формируем пакет СТРОГО по ТЗ
    severity_map = {"ok": "info", "late": "warning", "breakdown": "warning", "accident": "critical"}
    display_id = alert.vehicle_number or alert.vehicle_id
    location = alert.address or f"ТС {display_id}"

    payload = {
        "location": location,                  # 📍 Адрес или fallback
        "description": f"{alert.label}. {alert.comment}".strip(),
        "parameter": None,                     # 🎚 Пустой обязательно
        "value": None,
        "unit": None,
        "min_val": None,
        "max_val": None,
        "severity": severity_map.get(alert.alert_type, "info"),
        "meta": {
            "Водитель": alert.user_id,
            "Способ отправки": "Виджет водителя"
        },
        "context": {
            # "vehicle_id": alert.vehicle_id,
            
            # "coordinates": {"lat": alert.lat, "lon": alert.lon} if alert.lat else None,
            # "matrix_room": f"#bus_{alert.vehicle_id}:matrix.vpk-oil.ru"
            "ГосНомер": display_id      # 🚌 Номер автобуса
        }
    }

    # 3. 💾 Кладём в очередь
    payload["source"] = verified_source
    payload["timestamp"] = datetime.now().isoformat()
    
    redis_client.lpush("bus:all", json.dumps(payload, ensure_ascii=False))
    redis_client.ltrim("bus:all", 0, 1999)
    
    with open(os.path.join(LOG_DIR, "alerts_active.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        
    logger.info(f"🚛 Driver alert queued: {verified_source} | {location}")
    return {"status": "queued", "source": verified_source}

@app.get("/health")
def health(): return {"status": "ok", "redis": redis_client.ping()}