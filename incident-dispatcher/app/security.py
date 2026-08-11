from fastapi import Request, HTTPException, Header
from typing import List, Optional
from pydantic import BaseModel
import logging
from ipaddress import ip_address, ip_network
import httpx
import json
import os
from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("security")


# Загрузка локаций
LOCATIONS_CONFIG_FILE = "config/locations.json"
try:
    with open(LOCATIONS_CONFIG_FILE, "r", encoding="utf-8") as f:
        LOCATIONS = json.load(f)
    logger.info(f"✅ Loaded {len(LOCATIONS)} locations")
    logger.info(f"Items:\n {LOCATIONS.keys()} \n {LOCATIONS.values()}")
except Exception as e:
    logger.error(f"❌ Failed to load locations: {e}")
    LOCATIONS = {}

# Загрузка шаблонов оповещений
ALERT_TEMPLATES_FILE = "config/alert_templates.json"
try:
    with open(ALERT_TEMPLATES_FILE, "r", encoding="utf-8") as f:
        ALERT_CONFIG = json.load(f)
    logger.info(f"✅ Loaded {len(ALERT_CONFIG.get('audio_templates', []))} alert templates")
except Exception as e:
    logger.error(f"❌ Failed to load alert templates: {e}")
    ALERT_CONFIG = {"audio_templates": [], "location_events": {}}


CONFIG = {
    "hs": os.getenv("MATRIX_HOMESERVER"),
    "bot_token": os.getenv("MATRIX_BOT_TOKEN", os.getenv("MATRIX_ACCESS_TOKEN")),
    "dashboard_allowed_ips": [ip.strip() for ip in os.getenv("DASHBOARD_ALLOWED_IPS", "").split(",") if ip.strip()],
    "dashboard_room_id": os.getenv("DASHBOARD_MATRIX_ROOM_ID", ""),
    "dashboard_token": os.getenv("DASHBOARD_API_TOKEN"),  # ← Добавь это
    "auto_close_attention": int(os.getenv("AUTO_CLOSE_ATTENTION_MINUTES", "0")),
    "auto_close_alarm": int(os.getenv("AUTO_CLOSE_ALARM_MINUTES", "0")),
    "history_ttl_hours": int(os.getenv("HISTORY_TTL_HOURS", "48")),
    "timezone": os.getenv("TIMEZONE", "Asia/Novosibirsk"),
    "redis_url": os.getenv("REDIS_URL", "redis://redis:6379/4"),
    "asterisk_api_token": os.getenv("ASTERISK_API_TOKEN", ""),
    "dispatcher_write_token": os.getenv("DISPATCHER_WRITE_TOKEN", ""),
    "voice_api_url": os.getenv("VOICE_API_URL", "http://voice-api:8000"),
    "voice_api_secret": os.getenv("VOICE_API_SECRET", ""),
    "local_api_token": os.getenv("VOICE_API_SECRET", ""),
    "voice_api_secret": os.getenv("VOICE_API_SECRET", ""),
}

class MatrixUser(BaseModel):
    """Объект, который возвращает зависимость get_matrix_user"""
    user_id: str
    is_moderator: bool

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"

def check_ip_whitelist(request: Request, allowed_ips: List[str]) -> bool:
    if not allowed_ips:
        return True
    client_ip = get_client_ip(request)
    try:
        client_addr = ip_address(client_ip)
    except ValueError:
        return False
    for allowed in allowed_ips:
        allowed = allowed.strip()
        if '/' in allowed:
            if client_addr in ip_network(allowed, strict=False):
                return True
        else:
            if client_ip == allowed:
                return True
    return False

def get_template_by_id(template_id: str) -> Optional[dict]:
    """Найти шаблон по ID"""
    for template in ALERT_CONFIG.get("audio_templates", []):
        if template["id"] == template_id:
            return template
    return None

def get_location_events(loc_id: str) -> list:
    """Получить список событий для локации"""
    return ALERT_CONFIG.get("location_events", {}).get(loc_id, [])




async def verify_dashboard_access(
    request: Request, 
    authorization: Optional[str] = Header(default=None, alias="Authorization")
):
    """Проверка доступа к дашборду: IP ИЛИ (токен + модератор в комнате админов)"""
    if check_ip_whitelist(request, CONFIG["dashboard_allowed_ips"]):
        logger.info("Dashboard access granted by IP whitelist")
        return
    
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Доступ запрещен. Требуется авторизация Matrix")
    
    user_token = authorization.split(" ", 1)[1]
    # room_id = CONFIG["dashboard_room_id"]
    room_id = settings.DASHBOARD_MATRIX_ROOM_ID
    
    if not room_id:
        raise HTTPException(status_code=500, detail="DASHBOARD_MATRIX_ROOM_ID не настроен")
    
    is_mod, user_id = await verify_matrix_moderator(room_id, user_token)
    if not is_mod:
        raise HTTPException(status_code=403, detail=f"Пользователь {user_id} не является модератором")
    
    logger.info(f"Dashboard access granted for {user_id}")
    


    
# async def verify_location_access_or_dashboard(
#     request: Request,
#     loc_id: str,
#     authorization: Optional[str] = Header(default=None, alias="Authorization"),
#     x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
# ) -> MatrixUser:
#     """Проверка доступа: ИЛИ токен Matrix, ИЛИ глобальный токен дашборда + IP"""
#     loc = LOCATIONS.get(loc_id)
#     if not loc:
#         raise HTTPException(status_code=404, detail="Location not found")
    
#     logger.info(f"=== ПРОВЕРКА ДОСТУПА К {loc_id} ===")
#     logger.info(f"Authorization: {authorization[:20] if authorization else None}...")
#     logger.info(f"X-API-Token: {x_api_token[:20] if x_api_token else None}...")
    
#     # 🔥 Проверяем глобальный токен дашборда + IP
#     # dashboard_token = CONFIG.get("dashboard_token")
#     dashboard_token = settings.DASHBOARD_API_TOKEN
#     if x_api_token and dashboard_token and x_api_token == dashboard_token:
#         if check_ip_whitelist(request, CONFIG["dashboard_allowed_ips"]):
#             logger.info(f"✅ Доступ к {loc_id} разрешен по токену дашборда + IP")
#             return MatrixUser(user_id="dashboard", is_moderator=True)
#         else:
#             logger.error("❌ Токен дашборда верный, но IP не в whitelist")
#             raise HTTPException(status_code=403, detail="IP не в белом списке")
    
#     # Если не дашборд — проверяем токен Matrix
#     logger.info("Проверка через Matrix token...")
#     return await get_matrix_user_impl(request, loc_id, authorization, require_moderator=True)    


async def verify_location_access_or_dashboard(
    request: Request,
    loc_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
) -> MatrixUser:
    """Проверка доступа: ИЛИ токен Matrix, ИЛИ глобальный токен дашборда + IP, ИЛИ allowed_ips локации"""
    loc = LOCATIONS.get(loc_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    
    client_ip = get_client_ip(request)
    
    # 🔥 1. ПРОВЕРКА: IP входит в allowed_ips этой конкретной локации? (Цеховой пульт)
    allowed_ips = loc.get("allowed_ips", [])
    if allowed_ips and check_ip_whitelist(request, allowed_ips):
        logger.info(f"✅ Доступ к {loc_id} разрешен по allowed_ips для IP {client_ip}")
        return MatrixUser(user_id=f"local_desk_{loc_id}", is_moderator=True)
    
    # 2. ПРОВЕРКА: Глобальный токен дашборда + IP
    dashboard_token = settings.DASHBOARD_API_TOKEN
    if x_api_token and dashboard_token and x_api_token == dashboard_token:
        if check_ip_whitelist(request, CONFIG["dashboard_allowed_ips"]):
            logger.info(f"✅ Доступ к {loc_id} разрешен по токену дашборда + IP")
            return MatrixUser(user_id="dashboard", is_moderator=True)
        else:
            logger.error("❌ Токен дашборда верный, но IP не в whitelist")
            raise HTTPException(status_code=403, detail="IP не в белом списке дашборда")
    
    # 3. ПРОВЕРКА: Токен Matrix
    logger.info("Проверка через Matrix token...")
    return await get_matrix_user_impl(request, loc_id, authorization, require_moderator=True)

async def verify_matrix_moderator(room_id: str, user_token: str) -> tuple:
    """Проверяет, является ли пользователь модератором (>=50) в комнате"""
    async with httpx.AsyncClient() as client:
        whoami_resp = await client.get(
            # f"{CONFIG['hs']}/_matrix/client/v3/account/whoami",
            f"{settings.MATRIX_HOMESERVER}/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        if whoami_resp.status_code != 200:
            return False, ""
        user_id = whoami_resp.json().get("user_id")
        
        members_resp = await client.get(
            f"{settings.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{room_id}/joined_members",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        if members_resp.status_code != 200:
            return False, user_id
        
        members = members_resp.json().get("joined", {})
        user_data = members.get(user_id, {})
        power_level = user_data.get("power_level", 0)
        return power_level >= 50, user_id

async def get_matrix_user_impl(
    request: Request,
    loc_id: str,
    authorization: Optional[str],
    require_moderator: bool
) -> MatrixUser:
    """Универсальная проверка: токен + участие в комнате + опционально модератор"""
    loc = LOCATIONS.get(loc_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    
    # 🔥 ОТЛАДКА: Логируем все заголовки
    logger.info(f"=== ЗАПРОС К {loc_id} ===")
    logger.info(f"Все заголовки: {dict(request.headers)}")
    logger.info(f"Authorization header: {authorization}")
    logger.info(f"Require moderator: {require_moderator}")
    
    if not authorization or not authorization.startswith("Bearer "):
        logger.error("❌ Authorization header отсутствует или не начинается с 'Bearer '")
        raise HTTPException(status_code=401, detail="Требуется авторизация Matrix")
    
    user_token = authorization.split(" ", 1)[1]
    logger.info(f"Токен получен (первые 20 символов): {user_token[:20]}...")
    
    room_id = loc["room_id"]
    logger.info(f"Room ID: {room_id}")
    
    async with httpx.AsyncClient() as client:
        # 1. Узнаем, чей это токен
        logger.info(f"Запрос whoami к Matrix...")
        whoami_resp = await client.get(
            f"{settings.MATRIX_HOMESERVER}/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        logger.info(f"Whoami response status: {whoami_resp.status_code}")
        
        if whoami_resp.status_code != 200:
            logger.error(f"❌ Неверный токен: {whoami_resp.text}")
            raise HTTPException(status_code=401, detail="Неверный токен Matrix")
        
        user_id = whoami_resp.json().get("user_id")
        logger.info(f"User ID: {user_id}")
        
        # 2. Проверяем участие в комнате
        logger.info(f"Запрос joined_members...")
        members_resp = await client.get(
            f"{settings.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{room_id}/joined_members",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        logger.info(f"Members response status: {members_resp.status_code}")
        
        if members_resp.status_code != 200:
            logger.error(f"❌ Не удалось получить список участников: {members_resp.text}")
            raise HTTPException(status_code=403, detail="Не удалось получить список участников")
        
        members = members_resp.json().get("joined", {})
        logger.info(f"Всего участников в комнате: {len(members)}")
        logger.info(f"Пользователь {user_id} в списке: {user_id in members}")
        
        if user_id not in members:
            logger.error(f"❌ Пользователь не является участником комнаты")
            raise HTTPException(status_code=403, detail=f"Пользователь не является участником комнаты {loc['name']}")
        
        # 3. Проверяем уровень доступа
        power_level = members[user_id].get("power_level", 0)
        is_moderator = power_level >= 50
        
        logger.info(f"Power level: {power_level}, Is moderator: {is_moderator}")
        
        if require_moderator and not is_moderator:
            logger.error(f"❌ Требуются права модератора, но power_level={power_level}")
            raise HTTPException(status_code=403, detail=f"Требуются права модератора в комнате {loc['name']}")
        
        logger.info(f"✅ Доступ разрешен для {user_id}")
        return MatrixUser(user_id=user_id, is_moderator=is_moderator)
    

# Две обертки для Depends
async def get_matrix_user_read(
    request: Request,
    loc_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization")
) -> MatrixUser:
    return await get_matrix_user_impl(request, loc_id, authorization, require_moderator=False)

async def get_matrix_user_write(
    request: Request,
    loc_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization")
) -> MatrixUser:
    return await get_matrix_user_impl(request, loc_id, authorization, require_moderator=True)