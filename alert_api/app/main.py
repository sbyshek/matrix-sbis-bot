import os
import json
import asyncio
import time
import logging
from datetime import datetime
from typing import Optional, List, Dict
import redis
from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel

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

@app.get("/health")
def health(): return {"status": "ok", "redis": redis_client.ping()}