import os
import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, File, UploadFile, Form, Header, HTTPException, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
import redis.asyncio as redis
import aiofiles

from app.core.config import settings
from app.core.dependencies import verify_voice_secret

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
@app.get("/api/v1/campaign/next")
async def get_next_call(x_secret: str = Header(..., alias="X-Secret")):
    """
    Bash-скрипт Asterisk опрашивает этот эндпоинт, чтобы получить следующий номер для вызова.
    
    Возвращает:
    - "WAIT" — если достигнут лимит одновременных вызовов или очередь пуста
    - "OK|NUMBER|INITIATOR" — если есть задача (формат для удобного парсинга в bash)
    """
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    # Проверяем текущее количество активных вызовов
    active = await redis_client.get("voice:active") or 0
    active = int(active)
    
    if active >= settings.VOICE_MAX_CONCURRENT:
        return "WAIT"
    
    # Берём номер из очереди (LIFO — последний пришёл, первый ушёл)
    item = await redis_client.lpop("voice:queue")
    if not item:
        return "WAIT"
    
    # Формат элемента в очереди: "NUMBER|INITIATOR|CONTEXT"
    parts = item.split("|")
    number = parts[0]
    initiator = parts[1] if len(parts) > 1 else "unknown"
    
    # Увеличиваем счётчик активных вызовов
    await redis_client.incr("voice:active")
    
    # Ставим TTL на счётчик (страховка, если Asterisk не вернёт /finish)
    await redis_client.expire("voice:active", settings.ASTERISK_CALLBACK_TIMEOUT)
    
    logger.info(f"📞 Dispatched call to {number} (active: {active + 1}/{settings.VOICE_MAX_CONCURRENT})")
    
    # Возвращаем в формате, удобном для bash: OK|NUMBER|INITIATOR
    return f"OK|{number}|{initiator}"


@app.post("/api/v1/campaign/push")
async def push_numbers(request: Request, x_secret: str = Header(..., alias="X-Secret")):
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    data = await request.json()
    numbers_data = data.get("numbers", [])
    prefix = data.get("prefix", "unknown")
    context = data.get("context", "pa_call_file")
    
    count = 0
    for item in numbers_data:
        num = ''.join(filter(str.isdigit, str(item.get("number", ""))))
        note = item.get("note", "Без примечания")
        
        if 10 <= len(num) <= 15:
            # Формат очереди: number|note|prefix|context
            payload = f"{num}|{note}|{prefix}|{context}"
            await redis_client.lpush("voice:queue", payload)
            count += 1
            
    return {
        "status": "queued",
        "count": count,
        "queue_size": await redis_client.llen("voice:queue"),
        "prefix": prefix
    }

@app.post("/api/v1/campaign/finish")
async def finish_call(
    request: Request,
    x_secret: str = Header(..., alias="X-Secret")
):
    """
    Asterisk вызывает этот эндпоинт после завершения звонка.
    Это сбрасывает счётчик активных вызовов, позволяя запустить следующий.
    """
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    data = await request.form()
    number = data.get("number", "unknown")
    
    # Уменьшаем счётчик (но не ниже 0)
    current = await redis_client.decr("voice:active")
    if current < 0:
        await redis_client.set("voice:active", 0)
        current = 0
    
    logger.info(f"✅ Call to {number} finished (active: {current}/{settings.VOICE_MAX_CONCURRENT})")
    
    return {"status": "ok", "active": current}


# =============================================================================
# 🎙️ Voice Feedback Receiver (приём WAV от Asterisk)
# =============================================================================
@app.post("/api/v1/feedback")
async def receive_feedback(
    file: UploadFile = File(...),
    caller_id: str = Form(...),
    initiator: str = Form(...),
    x_secret: str = Header(..., alias="X-Secret"),
    background_tasks: BackgroundTasks = None
):
    """
    Принимает WAV-файл с голосовым ответом абонента.
    Сохраняет файл и ставит задачу на транскрипцию + отправку в Matrix.
    """
    verify_voice_secret(x_secret, settings.VOICE_API_SECRET)
    
    # Валидация caller_id (только цифры, 10-15 знаков)
    if not caller_id.isdigit() or not (10 <= len(caller_id) <= 15):
        raise HTTPException(400, detail="Invalid caller_id format")
    
    # Генерируем уникальное имя файла
    task_id = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{task_id}_{caller_id}_{timestamp}.wav"
    filepath = Path(settings.VOICE_DATA_DIR) / filename
    
    # Сохраняем файл асинхронно
    try:
        async with aiofiles.open(filepath, 'wb') as f:
            content = await file.read()
            await f.write(content)
        logger.info(f"💾 Saved feedback: {filepath} ({len(content)} bytes)")
    except Exception as e:
        logger.error(f"❌ Failed to save file: {e}")
        raise HTTPException(500, detail="Failed to save audio file")
    
    # Ставим фоновую задачу на обработку (STT → Matrix)
    if background_tasks:
        background_tasks.add_task(
            process_voice_feedback,
            filepath=str(filepath),
            caller_id=caller_id,
            initiator=initiator,
            task_id=task_id
        )
        logger.info(f"🔄 Queued STT task for {task_id}")
    
    return {
        "status": "accepted",
        "task_id": task_id,
        "caller_id": caller_id,
        "filename": filename
    }


# =============================================================================
# 🧠 Фоновая обработка: STT + Matrix
# =============================================================================
async def process_voice_feedback(
    filepath: str,
    caller_id: str,
    initiator: str,
    task_id: str
):
    """
    Фоновая задача: транскрибирует аудио и отправляет результат в Matrix.
    """
    logger.info(f"🎙️ Processing feedback: {task_id} ({caller_id})")
    
    try:
        # 1. Транскрипция (если модель загружена)
        text = ""
        if model:
            logger.info(f"🔊 Running Whisper on {filepath}")
            segments, _ = model.transcribe(
                filepath,
                language=settings.WHISPER_LANGUAGE,
                prompt=settings.WHISPER_PROMPT,
                vad_filter=True,  # Убирает тишину
                vad_parameters=dict(min_silence_duration_ms=500)
            )
            text = " ".join([segment.text.strip() for segment in segments]).strip()
            logger.info(f"📝 Transcribed: {text[:100]}..." if len(text) > 100 else f"📝 Transcribed: {text}")
        else:
            text = "[STT не доступен]"
            logger.warning("⚠️ Skipping STT: model not loaded")
        
        # 2. Отправка в Matrix (если настроено)
        if settings.MATRIX_BOT_TOKEN and settings.MATRIX_FEEDBACK_ROOM_ID:
            await send_to_matrix(
                caller_id=caller_id,
                initiator=initiator,
                text=text,
                audio_filename=Path(filepath).name,
                task_id=task_id
            )
            logger.info(f"✅ Posted to Matrix room {settings.MATRIX_FEEDBACK_ROOM_ID}")
        
        # 3. Очистка файла (по истечении retention)
        # В продакшене лучше запускать отдельный cron-воркер для массового удаления
        # Здесь для простоты удаляем сразу после успешной обработки
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"🗑️ Deleted temporary file: {filepath}")
            
    except Exception as e:
        logger.error(f"❌ Error processing feedback {task_id}: {e}", exc_info=True)
        # Не выбрасываем исключение, чтобы не ломать основной поток
        # Можно добавить повторную попытку или алерт в Matrix


# =============================================================================
# 🔊 Matrix Integration
# =============================================================================
async def send_to_matrix(
    caller_id: str,
    initiator: str,
    text: str,
    audio_filename: str,
    task_id: str
):
    """Отправляет карточку с голосовым ответом в Matrix-комнату"""
    import httpx
    
    # Формируем HTML-сообщение
    html_body = f"""
    🎙️ <b>Голосовой ответ</b><br>
    📞 Абонент: <code>{caller_id}</code><br>
    👤 Инициатор: {initiator}<br>
    📝 Текст: {text}<br>
    🎧 Аудио: <a href="https://vpk-oil.ru/voice/{audio_filename}">скачать</a><br>
    ⏰ {datetime.now(timezone.utc).strftime("%H:%M:%S")}
    """
    
    payload = {
        "msgtype": "m.text",
        "format": "org.matrix.custom.html",
        "body": f"Голосовой ответ от {caller_id}: {text}",
        "formatted_body": html_body.strip()
    }
    
    url = f"{settings.MATRIX_HOMESERVER}/_matrix/client/r0/rooms/{settings.MATRIX_FEEDBACK_ROOM_ID}/send/m.room.message"
    headers = {
        "Authorization": f"Bearer {settings.MATRIX_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()


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