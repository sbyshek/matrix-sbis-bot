"""
Аудио-хранилище диспетчера.

Хранит файлы оповещений рядом с шаблонами, конвертирует любые
загруженные файлы в телефонный формат (WAV, PCM 16-bit, mono, 8000 Hz)
и отдаёт их Asterisk по HTTP.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.alert_svc import TEMPLATES
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])

AUDIO_DIR = Path("data/audio")
CUSTOM_AUDIO_DIR = AUDIO_DIR / "custom"
TEMPLATES_FILE = Path("config/templates.json")

AUDIO_DIR.mkdir(parents=True, exist_ok=True)
CUSTOM_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Хелперы
# =============================================================================
def _token_or_403(*tokens) -> str:
    token = next((t for t in tokens if t), None)
    if not token:
        raise HTTPException(status_code=403, detail="Token required")
    if token not in (
        settings.LOCAL_API_TOKEN,
        settings.DASHBOARD_API_TOKEN,
        settings.VOICE_API_SECRET,
    ):
        raise HTTPException(status_code=403, detail="Invalid token")
    return token


def _resolve_audio_path(filename: str) -> Path:
    """Безопасно резолвит путь ВНУТРИ AUDIO_DIR (защита от ../)."""
    rel = Path(filename)
    if ".." in rel.parts:
        raise HTTPException(status_code=400, detail="Invalid filename")

    target = AUDIO_DIR / rel
    if target.suffix.lower() not in (".wav", ".gsm", ".sln"):
        target = target.with_suffix(".wav")

    target = target.resolve()
    if not str(target).startswith(str(AUDIO_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

    return target


# =============================================================================
# ЗАГРУЗКА + КОНВЕРТАЦИЯ (важно: объявлена ДО catch-all GET!)
# =============================================================================
@router.post("/api/v1/audio/upload")
async def upload_audio(
    file: UploadFile = File(...),
    template_id: str = Form(default=None),
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
):
    """
    Принимает ЛЮБОЙ аудиофайл (webm/mp3/wav/ogg...) и конвертирует
    в телефонный формат: WAV, PCM 16-bit, mono, 8000 Hz.

    Опционально привязывает файл к шаблону (template_id).
    """
    _token_or_403(x_local_token, x_api_token)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"test_{ts}"
    raw_path = Path(f"/tmp/upload_{base_name}_{uuid.uuid4().hex[:6]}")
    out_path = CUSTOM_AUDIO_DIR / f"{base_name}.wav"

    try:
        async with aiofiles.open(raw_path, "wb") as f:
            await f.write(await file.read())

        # 🔥 Конвертация в формат Asterisk
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(raw_path),
            "-ar", "8000", "-ac", "1", "-c:a", "pcm_s16le", str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"❌ ffmpeg error: {stderr.decode()[-500:]}")
            raise HTTPException(status_code=500, detail="Audio conversion failed")

    finally:
        raw_path.unlink(missing_ok=True)

    # 🔥 ВАЖНО: возвращаем БЕЗ расширения — dialplan сам добавит .wav
    audio_file = f"custom/{base_name}"

    # Опциональная привязка к шаблону
    if template_id and template_id in TEMPLATES:
        TEMPLATES[template_id]["audio_file"] = audio_file
        try:
            with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
                json.dump(TEMPLATES, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ Template {template_id} linked to {audio_file}")
        except Exception as e:
            logger.error(f"❌ Failed to update template: {e}")

    logger.info(f"✅ Audio uploaded: {audio_file} ({out_path.stat().st_size} bytes)")

    return {
        "status": "success",
        "audio_file": audio_file,
        "filename": f"{base_name}.wav",
        "size": out_path.stat().st_size,
    }


# =============================================================================
# СПИСОК ФАЙЛОВ
# =============================================================================
@router.get("/api/v1/audio/list")
async def list_audio(
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
):
    _token_or_403(x_local_token, x_api_token)

    files = []
    for p in sorted(AUDIO_DIR.rglob("*.wav")):
        files.append({
            "audio_file": str(p.relative_to(AUDIO_DIR).with_suffix("")),
            "filename": p.name,
            "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
        })

    return {"files": files}


# =============================================================================
# УДАЛЕНИЕ
# =============================================================================
@router.delete("/api/v1/audio/{filename:path}")
async def delete_audio(
    filename: str,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
):
    _token_or_403(x_local_token, x_api_token)

    target = _resolve_audio_path(filename)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

        # 🔥 Защита: нельзя удалить файл, привязанный к шаблону
    key = str(Path(filename).with_suffix(""))
    used_by = [
        tid for tid, tpl in TEMPLATES.items()
        if str(tpl.get("audio_file", "")).strip() == key
    ]
    if used_by:
        raise HTTPException(
            status_code=409,
            detail=f"Файл используется шаблонами: {', '.join(used_by)}. Сначала отвяжите его."
        )

    
    target.unlink()
    logger.info(f"🗑️ Audio deleted: {filename}")

    return {"status": "success"}


# =============================================================================
# ОТДАЧА ФАЙЛА (Asterisk тянет отсюда) — ОБЪЯВЛЯТЬ ПОСЛЕДНИМ (catch-all)!
# =============================================================================
@router.get("/api/v1/audio/{filename:path}")
async def get_audio(
    filename: str,
    x_secret: str = Header(default=None, alias="X-Secret"),
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
):
    """
    Отдаёт аудиофайл Asterisk / внутренним сервисам.
    Запрос от Asterisk: GET /api/v1/audio/custom/test_xxx.wav + X-Secret.
    """
    _token_or_403(x_secret, x_local_token, x_api_token)

    target = _resolve_audio_path(filename)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(target, media_type="audio/wav")