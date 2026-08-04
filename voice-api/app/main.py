import os
import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
import json
from fastapi import FastAPI, Request, File, UploadFile, Form, Header, HTTPException, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
import redis.asyncio as redis
import aiofiles
import httpx
from app.core.config import settings
from app.core.dependencies import verify_voice_secret
from app.services.asterisk_manager import asterisk_manager
from app.core.subscribers import parse_subscribers_list, Subscriber, parse_subscriber

from pydantic import BaseModel


class CampaignTriggerRequest(BaseModel):
    node_id: str
    template_id: str
    audio_file: str
    text_for_matrix: str
    subscribers: list[str]  # Формат: "number:code:description"
    loc_id: str
    loc_name: str
    initiator: str

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("voice-api")

app = FastAPI(
    title="Voice Feedback API",
    description="Приём голосовых ответов от Asterisk → STT → Matrix",
    version="1.0.0"
)

# Глобальные объекты
redis_client: redis.Redis = None
model = None  # Будет инициализирована позже (faster-whisper)


# =============================================================================
# 🚀 Startup / Shutdown
# =============================================================================
@app.on_event("startup")
async def startup_event():
    global redis_client, model
    logger.info("🚀 Starting Voice Feedback API...")
    
    # Подключение к Redis
    redis_client = redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5
    )
    
    # Проверка подключения
    try:
        await asterisk_manager.connect_all()
        await redis_client.ping()
        logger.info("✅ Connected to Redis")
    except Exception as e:
        logger.error(f"❌ Redis connection failed: {e}")
        raise
    
    # Инициализация STT модели (отложено, если модель тяжёлая)
    if settings.WHISPER_MODEL and not model:
        logger.info(f"🎙️ Loading Whisper model: {settings.WHISPER_MODEL} ({settings.WHISPER_COMPUTE_TYPE})")
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(
                settings.WHISPER_MODEL,
                device=settings.WHISPER_DEVICE,
                compute_type=settings.WHISPER_COMPUTE_TYPE,
                language=settings.WHISPER_LANGUAGE
            )
            logger.info("✅ Whisper model loaded")
        except ImportError:
            logger.warning("⚠️ faster-whisper not installed. STT will be disabled.")
        except Exception as e:
            logger.error(f"❌ Failed to load Whisper model: {e}")
    
    # Создаём папку для данных
    Path(settings.VOICE_DATA_DIR).mkdir(parents=True, exist_ok=True)
    logger.info(f"📁 Data directory: {settings.VOICE_DATA_DIR}")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Shutting down...")
    if redis_client:
        await redis_client.close()


# =============================================================================
# 🔍 Health Check
# =============================================================================
@app.get("/health")
async def health_check():
    """Простая проверка работоспособности сервиса"""
    status = {"status": "ok", "service": "voice-api", "version": "1.0.0"}
    
    # Проверка Redis
    try:
        await redis_client.ping()
        status["redis"] = "connected"
    except:
        status["redis"] = "disconnected"
        status["status"] = "degraded"
    
    # Проверка STT модели
    status["stt"] = "loaded" if model else "not_loaded"
    
    return JSONResponse(status, status_code=200 if status["status"] == "ok" else 503)


# =============================================================================
# 📞 Campaign Dispatcher (для Bash-скрипта Asterisk)
# =============================================================================
# @app.get("/api/v1/campaign/next")
# async def get_next_call(x_secret: str = Header(..., alias="X-Secret")):
#     """
#     Bash-скрипт Asterisk опрашивает этот эндпоинт, чтобы получить следующий номер для вызова.
    
#     Возвращает:
#     - "WAIT" — если достигнут лимит одновременных вызовов или очередь пуста
#     - "OK|NUMBER|INITIATOR" — если есть задача (формат для удобного парсинга в bash)
#     """
#     verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
#     # Проверяем текущее количество активных вызовов
#     active = await redis_client.get("voice:active") or 0
#     active = int(active)
    
#     if active >= settings.VOICE_MAX_CONCURRENT:
#         return "WAIT"
    
#     # Берём номер из очереди (LIFO — последний пришёл, первый ушёл)
#     item = await redis_client.lpop("voice:queue")
#     if not item:
#         return "WAIT"
    
#     # Формат элемента в очереди: "NUMBER|INITIATOR|CONTEXT"
#     parts = item.split("|")
#     number = parts[0]
#     initiator = parts[1] if len(parts) > 1 else "unknown"
#     prefix = parts[2] if len(parts) > 2 else "unknown"
    
#     # Увеличиваем счётчик активных вызовов
#     await redis_client.incr("voice:active")
    
#     # Ставим TTL на счётчик (страховка, если Asterisk не вернёт /finish)
#     await redis_client.expire("voice:active", settings.ASTERISK_CALLBACK_TIMEOUT)
    
#     logger.info(f"📞 Dispatched call to {number} (active: {active + 1}/{settings.VOICE_MAX_CONCURRENT})")
    
#     # Возвращаем в формате, удобном для bash: OK|NUMBER|INITIATOR
#     return f"OK|{number}|{initiator}|{prefix}"


@app.get("/api/v1/campaign/next")
async def get_next_call(x_secret: str = Header(..., alias="X-Secret")):
    """
    Возвращает следующий номер для вызова.
    Формат ответа: "OK|NUMBER|CODE|DESCRIPTION|INITIATOR"
    """
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    # Проверяем лимит одновременных вызовов
    active = await redis_client.get("voice:active") or 0
    active = int(active)
    
    if active >= settings.VOICE_MAX_CONCURRENT:
        return "WAIT"
    
    # Берём элемент из очереди
    item = await redis_client.lpop("voice:queue")
    if not item:
        return "WAIT"
    
    # Формат: "NUMBER|CODE|DESCRIPTION|PREFIX|CONTEXT"
    parts = item.split("|")
    number = parts[0]
    code = parts[1] if len(parts) > 1 else ""
    description = parts[2] if len(parts) > 2 else ""
    prefix = parts[3] if len(parts) > 3 else "unknown"
    context = parts[4] if len(parts) > 4 else "pa_call_file"
    
    # Увеличиваем счётчик активных вызовов
    await redis_client.incr("voice:active")
    await redis_client.expire("voice:active", settings.ASTERISK_CALLBACK_TIMEOUT)
    
    # Возвращаем расширенный формат
    initiator = f"{description} ({code})" if description and code else (description or code or "unknown")
    
    logger.info(f"📞 Dispatched call to {number} ({initiator}) via prefix {prefix}")
    
    return f"OK|{number}|{code}|{description}|{initiator}"

# @app.post("/api/v1/campaign/push")
# async def push_numbers(request: Request, x_secret: str = Header(..., alias="X-Secret")):
#     verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
#     data = await request.json()
#     numbers_data = data.get("numbers", [])
#     prefix = data.get("prefix", "unknown")
#     context = data.get("context", "pa_call_file")
    
#     count = 0
#     for item in numbers_data:
#         num = ''.join(filter(str.isdigit, str(item.get("number", ""))))
#         note = item.get("note", "Без примечания")
        
#         if 10 <= len(num) <= 15:
#             # Формат очереди: number|note|prefix|context
#             payload = f"{num}|{note}|{prefix}|{context}"
#             await redis_client.lpush("voice:queue", payload)
#             count += 1
            
#     return {
#         "status": "queued",
#         "count": count,
#         "queue_size": await redis_client.llen("voice:queue"),
#         "prefix": prefix
#     }

@app.post("/api/v1/campaign/push")
async def push_numbers(
    request: Request,
    x_secret: str = Header(..., alias="X-Secret")
):
    """
    Принимает список номеров для обзвона.
    Поддерживает формат "number:code:description" для каждого номера.
    """
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    data = await request.json()
    numbers_data = data.get("numbers", [])
    prefix = data.get("prefix", "unknown")
    context = data.get("context", "pa_call_file")
    
    count = 0
    parsed_subscribers = []
    
    for item in numbers_data:
        # Поддерживаем два формата:
        # 1. {"number": "79001112233:boss:Иванов", "note": "..."}
        # 2. {"number": "79001112233", "code": "boss", "description": "Иванов", "note": "..."}
        
        raw_number = str(item.get("number", ""))
        note = item.get("note", "")
        
        # Если number содержит ":", парсим как составную строку
        if ":" in raw_number:
            try:
                sub = parse_subscriber(raw_number)
                # Если есть note, используем его как description (если description пуст)
                if note and not sub.description:
                    sub.description = note
            except ValueError as e:
                logger.warning(f"⚠️ Invalid subscriber format: {raw_number} - {e}")
                continue
        else:
            # Старый формат: только номер
            code = item.get("code", "")
            description = item.get("description", note)
            sub = Subscriber(
                number=''.join(filter(str.isdigit, raw_number)),
                code=code,
                description=description
            )
        
        if not sub.number or len(sub.number) < 3:
            logger.warning(f"⚠️ Skipping invalid number: {raw_number}")
            continue
        
        # Формат очереди: number|code|description|prefix|context
        payload = f"{sub.number}|{sub.code}|{sub.description}|{prefix}|{context}"
        await redis_client.lpush("voice:queue", payload)
        count += 1
        parsed_subscribers.append(sub)
    
    logger.info(f"📥 Queued {count} subscribers for prefix {prefix}")
    
    return {
        "status": "queued",
        "count": count,
        "queue_size": await redis_client.llen("voice:queue"),
        "prefix": prefix,
        "subscribers": [str(sub) for sub in parsed_subscribers]
    }

# =============================================================================
# 1. ПУЛЕНЕПРОБИВАЕМЫЙ FINISH (Больше никаких 422)
# =============================================================================
@app.post("/api/v1/campaign/finish")
async def finish_call(
    number: str = Form(default="unknown"),       # Если Asterisk пришлёт пустоту, будет "unknown"
    status: str = Form(default="completed"),     # По умолчанию
    trunk: str = Form(default="unknown"),
    event: str = Form(default="call_finished"),
    campaign_id: str = Form(default="unknown"),
    x_secret: str = Header(..., alias="X-Secret")
):
    """
    Asterisk вызывает этот эндпоинт после завершения звонка.
    Мы логируем факт звонка в JSONL и сбрасываем счётчик.
    """
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    # Логируем в JSONL (как ты и хотел, это надёжно и просто)
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "number": number,
        "trunk": trunk,
        "status": status,
        "event": "call_finished",
        "campaign_id": campaign_id
    }
    
    log_file = Path(settings.VOICE_DATA_DIR) / "calls.log.jsonl"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        logger.info(f"📝 Logged call to {number} (status: {status}) in JSONL")
    except Exception as e:
        logger.error(f"❌ Failed to write to JSONL: {e}")
    
    # Сбрасываем счётчик активных вызовов
    # current = await redis_client.decr("voice:active")
    # if current < 0:
    #     await redis_client.set("voice:active", 0)
    #     current = 0
    
    if event == "call_finished" and trunk != "unknown" and trunk != "internal":
        current = await redis_client.decr(f"voice:active_trunk:{trunk}")
        if current < 0:
            await redis_client.set(f"voice:active_trunk:{trunk}", 0)
        
    return {"status": "ok", "event": event}


@app.get("/api/v1/campaign/{campaign_id}/logs")
async def get_campaign_logs(
    campaign_id: str,
    x_secret: str = Header(..., alias="X-Secret")
):
    """Возвращает все логи конкретной кампании"""
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    log_file = Path(settings.VOICE_DATA_DIR) / "calls.log.jsonl"
    if not log_file.exists():
        return {"campaign_id": campaign_id, "total_events": 0, "events": []}
    
    events = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("campaign_id") == campaign_id:
                    events.append(entry)
            except json.JSONDecodeError:
                continue
    
    return {
        "campaign_id": campaign_id,
        "total_events": len(events),
        "events": events
    }


# =============================================================================
# 2. МЯГКИЙ FEEDBACK (На случай, если Asterisk всё же решит отправить файл)
# =============================================================================
@app.post("/api/v1/feedback")
async def receive_feedback(
    file: UploadFile = File(...),
    caller_id: str = Form(default="unknown"),    # Дефолтное значение спасает от 422
    initiator: str = Form(default="unknown"),
    exten: str = Form(default="unknown"),        # Добавлено, чтобы не было 422 из-за лишнего поля
    x_secret: str = Header(..., alias="X-Secret"),
    background_tasks: BackgroundTasks = None
):
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    # 🔥 МЯГКАЯ ВАЛИДАЦИЯ: Разрешаем любые номера от 3 цифр (для внутренних 4499 и т.д.)
    if not caller_id.isdigit() or len(caller_id) < 3:
        logger.warning(f"⚠️ Suspicious caller_id: '{caller_id}', but processing anyway.")
        # Мы НЕ выбрасываем HTTPException, чтобы не ломать поток, просто логируем
    
    task_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{task_id}_{caller_id}_{timestamp}.wav"
    filepath = Path(settings.VOICE_DATA_DIR) / filename
    
    try:
        async with aiofiles.open(filepath, 'wb') as f:
            content = await file.read()
            await f.write(content)
        logger.info(f"💾 Saved feedback: {filepath} ({len(content)} bytes)")
    except Exception as e:
        logger.error(f"❌ Failed to save file: {e}")
        raise HTTPException(500, detail="Failed to save audio file")
    
    if background_tasks:
        background_tasks.add_task(
            process_voice_feedback,
            filepath=str(filepath),
            caller_id=caller_id,
            initiator=initiator,
            exten=exten,
            task_id=task_id
        )
        logger.info(f"🔄 Queued STT task for {task_id}")
    
    return {"status": "accepted", "task_id": task_id, "caller_id": caller_id}
# @app.post("/api/v1/campaign/trigger-template")
# async def trigger_template_campaign(
#     request: CampaignTriggerRequest,
#     x_secret: str = Header(..., alias="X-Secret")
# ):
#     """
#     Запуск кампании голосового оповещения по шаблону.
#     Вызывается из incident-dispatcher.
#     """
#     verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
#     # Парсим список абонентов
#     subscribers = parse_subscribers_list(request.subscribers)
    
#     if not subscribers:
#         raise HTTPException(400, detail="No valid subscribers in the list")
    
#     logger.info(
#         f"🚀 Triggering campaign: template={request.template_id}, "
#         f"location={request.loc_name}, subscribers={len(subscribers)}"
#     )
    
#     # Логирование начала кампании
#     log_entry = {
#         "timestamp": datetime.now(timezone.utc).isoformat(),
#         "event": "start_campaign",
#         "status": "new",
#         "template_id": request.template_id,
#         "loc_id": request.loc_id,
#         "loc_name": request.loc_name,
#         "total_subscribers": len(subscribers),
#         "node_id": request.node_id,
#         "initiator": request.initiator
#     }
    
#     log_file = Path(settings.VOICE_DATA_DIR) / "calls.log.jsonl"
#     try:
#         with open(log_file, "a", encoding="utf-8") as f:
#             f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
#         logger.info(f"🚀 Logged campaign start: {request.template_id} for {request.loc_name}")
#     except Exception as e:
#         logger.error(f"❌ Failed to write campaign start to JSONL: {e}")
    
#     # logger.info(f"🚀 Triggering campaign: template={request.template_id}, location={request.loc_name}, subscribers={len(subscribers)}")
    
    
#     # Запускаем кампанию через AMI
#     results = await asterisk_manager.originate_campaign(
#         node_id=request.node_id,
#         subscribers=subscribers,
#         audio_file=request.audio_file,
#         initiator=request.initiator,
#         redis_client=redis_client
#     )
    
#     return {
#         "status": "success",
#         "template_id": request.template_id,
#         "location": request.loc_name,
#         "total_subscribers": len(subscribers),
#         "results": results
#     }


@app.post("/api/v1/campaign/trigger-template")
async def trigger_template_campaign(
    request: CampaignTriggerRequest,
    x_secret: str = Header(..., alias="X-Secret")
):
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    # 🔥 Генерируем уникальный ID кампании
    campaign_id = uuid.uuid4().hex
    
    subscribers = parse_subscribers_list(request.subscribers)
    if not subscribers:
        raise HTTPException(400, detail="No valid subscribers in the list")
    
    # 🔥 Логируем старт кампании с campaign_id
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "start_campaign",
        "status": "new",
        "campaign_id": campaign_id,  # 🔥 НОВОЕ ПОЛЕ
        "template_id": request.template_id,
        "loc_id": request.loc_id,
        "loc_name": request.loc_name,
        "total_subscribers": len(subscribers),
        "node_id": request.node_id,
        "initiator": request.initiator
    }
    
    log_file = Path(settings.VOICE_DATA_DIR) / "calls.log.jsonl"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        logger.info(f"🚀 Logged campaign start: {campaign_id} - {request.template_id} for {request.loc_name}")
    except Exception as e:
        logger.error(f"❌ Failed to write campaign start to JSONL: {e}")
    
    logger.info(f"🚀 Triggering campaign: {campaign_id}, template={request.template_id}, location={request.loc_name}, subscribers={len(subscribers)}")
    
    # 🔥 Передаём campaign_id в originate_campaign
    results = await asterisk_manager.originate_campaign(
        node_id=request.node_id,
        subscribers=subscribers,
        audio_file=request.audio_file,
        initiator=request.initiator,
        redis_client=redis_client,
        campaign_id=campaign_id  # 🔥 НОВОЕ ПОЛЕ
    )
    
    return {
        "status": "success",
        "campaign_id": campaign_id,  # 🔥 Возвращаем ID, чтобы диспетчер мог сохранить
        "template_id": request.template_id,
        "location": request.loc_name,
        "total_subscribers": len(subscribers),
        "results": results
    }



# =============================================================================
# 🧠 Фоновая обработка: STT + Matrix
# =============================================================================
# async def process_voice_feedback(
#     filepath: str,
#     caller_id: str,
#     initiator: str,
#     exten: str,      # 🔥 Новое поле
#     task_id: str
# ):
#     logger.info(f"🎙️ Processing feedback: {task_id} ({caller_id}) for exten: {exten}")
#     try:
#         # 1. Транскрипция (без изменений)
#         text = ""
#         if model:
#             logger.info(f"🔊 Running Whisper on {filepath}")
#             segments, _ = model.transcribe(
#                 filepath,
#                 language=settings.WHISPER_LANGUAGE,
#                 prompt=settings.WHISPER_PROMPT,
#                 vad_filter=True,
#                 vad_parameters=dict(min_silence_duration_ms=500)
#             )
#             text = " ".join([segment.text.strip() for segment in segments]).strip()
#             logger.info(f"📝 Transcribed: {text[:100]}..." if len(text) > 100 else f"📝 Transcribed: {text}")
#         else:
#             text = "[STT не доступен]"

#         # 2. 🔥 НОВАЯ ЛОГИКА: Отправляем в incident-dispatcher (вместо Matrix)
#         if text and text != "[STT не доступен]":
#             try:
#                 async with httpx.AsyncClient(timeout=15.0) as client:
#                     payload = {
#                         "exten": exten,
#                         "caller_id": caller_id,
#                         "initiator": initiator,
#                         "comment": text
#                     }
#                     headers = {
#                         "X-API-Token": settings.INCIDENT_DISPATCHER_TOKEN,
#                         "Content-Type": "application/json"
#                     }
#                     url = f"{settings.INCIDENT_DISPATCHER_URL}/api/v1/incident/voice-trigger"
                    
#                     response = await client.post(url, json=payload, headers=headers)
                    
#                     if response.status_code == 200:
#                         logger.info(f"✅ Successfully routed incident to dispatcher for exten {exten}")
#                     else:
#                         logger.error(f"❌ Dispatcher rejected request: {response.status_code} - {response.text}")
                        
#             except Exception as e:
#                 logger.error(f"❌ Failed to call incident-dispatcher: {e}")
#         else:
#             logger.warning("⚠️ No text transcribed, skipping incident creation")

#         # 3. Очистка файла
#         if os.path.exists(filepath):
#             os.remove(filepath)
#             logger.info(f"🗑️ Deleted temporary file: {filepath}")

#     except Exception as e:
#         logger.error(f"❌ Error processing feedback {task_id}: {e}", exc_info=True)



# =============================================================================
# 🔧 Debug / Admin endpoints (опционально, можно убрать в prod)
# =============================================================================
@app.get("/api/v1/debug/queue")
async def debug_queue(x_secret: str = Header(..., alias="X-Secret")):
    """Показывает текущее состояние очереди (для отладки)"""
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    queue_len = await redis_client.llen("voice:queue")
    active = await redis_client.get("voice:active") or 0
    
    return {
        "queue_length": queue_len,
        "active_calls": int(active),
        "max_concurrent": settings.VOICE_MAX_CONCURRENT
    }


@app.delete("/api/v1/debug/reset")
async def debug_reset(x_secret: str = Header(..., alias="X-Secret")):
    """Сбрасывает счётчик активных вызовов (для экстренных случаев)"""
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    await redis_client.set("voice:active", 0)
    logger.warning("🔄 Active calls counter reset by admin")
    
    return {"status": "reset", "active": 0}