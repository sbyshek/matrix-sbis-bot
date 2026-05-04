import os
import json
import time
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import redis


REDIS_KEY_ALL = "bus:all_events"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Universal Alert Bus", version="2.0")
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    # password=os.getenv("REDIS_PASSWORD"),
    decode_responses=True, 
    retry_on_timeout=True)

LOG_DIR = "/app/logs"
os.makedirs(LOG_DIR, exist_ok=True)
MAX_LOG_AGE_DAYS = 30

class Alert(BaseModel):
    # target_room: str
    source: str = "Unknown"
    location: Optional[str] = None
    description: Optional[str] = None
    parameter: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    severity: str = "info"

def get_active_log_path() -> str:
    return os.path.join(LOG_DIR, "alerts_active.jsonl")

def rotate_log_if_needed():
    path = get_active_log_path()
    if os.path.exists(path):
        created = os.path.getctime(path)
        age_days = (time.time() - created) / 86400
        if age_days > MAX_LOG_AGE_DAYS:
            timestamp = datetime.now().strftime("%Y-%m-%d")
            new_name = os.path.join(LOG_DIR, f"alerts_archive_{timestamp}.jsonl")
            os.rename(path, new_name)
            logger.info(f"📂 Log rotated: {new_name}")

async def rotation_scheduler():
    while True:
        try:
            rotate_log_if_needed()
        except Exception as e:
            logger.error(f"Rotation error: {e}")
        await asyncio.sleep(86400)


@app.post("/api/v1/ingest", status_code=201)
async def receive_alert(alert: Alert):
    alert_data = alert.model_dump()
    alert_data["timestamp"] = datetime.now().isoformat()
    
    # 1. Пишем в общий поток Redis
    redis_client.lpush(REDIS_KEY_ALL, json.dumps(alert_data))
    # Храним последние 2000 событий глобально
    redis_client.ltrim(REDIS_KEY_ALL, 0, 1999)
    
    # 2. Пишем в JSONL (логирование)
    with open(get_active_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(alert_data, ensure_ascii=False) + "\n")
        
    logger.info(f"✅ Event [{alert.severity}] from {alert.source}")
    return {"status": "ok"}

@app.get("/api/v1/stream/all")
async def get_all_stream(since: str = None, limit: int = 50):
    """Отдает события новее указанной метки времени"""
    items = redis_client.lrange(REDIS_KEY_ALL, 0, -1) # Берем всё (в Redis их немного)
    # Парсим и фильтруем
    events = []
    for item in items:
        evt = json.loads(item)
        if since is None or evt["timestamp"] > since:
            events.append(evt)
    
    # Возвращаем в обратном порядке (от старых к новым), чтобы бот отправлял правильно
    return list(reversed(events[:limit])) # limit на всякий случай

@app.get("/api/v1/stream/{room}")
async def get_stream(room: str, limit: int = 20):
    key = f"bus:{room}"
    items = redis_client.lrange(key, 0, limit - 1)
    return [json.loads(i) for i in items]

@app.on_event("startup")
async def startup():
    asyncio.create_task(rotation_scheduler())
    rotate_log_if_needed()


@app.get("/health")
def health():
    return {"status": "ok", "redis": redis_client.ping()}