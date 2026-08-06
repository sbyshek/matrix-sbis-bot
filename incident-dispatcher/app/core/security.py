from fastapi import Request, HTTPException, Header
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

def verify_local_token(x_local_token: str = Header(..., alias="X-Local-Token")):
    """Проверяет локальный API-токен для внутренних вызовов между сервисами"""
    if x_local_token != settings.LOCAL_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid local token")
    logger.info("✅ Local token verified")

def verify_ip(request: Request):
    """Проверяет, что запрос пришёл с разрешённого IP"""
    client_ip = request.client.host
    if client_ip not in settings.allowed_ips_list:
        raise HTTPException(status_code=403, detail=f"IP {client_ip} not allowed")
    logger.info(f"✅ IP {client_ip} verified")
    
    
def verify_ui_access(
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    """
    Универсальная проверка для UI-эндпоинтов.
    Принимает либо локальный токен (сервис-сервис), либо дашбордный токен (UI).
    """
    # Пробуем локальный токен
    if x_local_token and x_local_token == settings.LOCAL_API_TOKEN:
        logger.info("✅ UI access granted via X-Local-Token")
        verify_ip(request)
        return
    
    # Пробуем дашбордный токен
    if x_api_token and x_api_token == settings.DASHBOARD_API_TOKEN:
        logger.info("✅ UI access granted via X-API-Token (dashboard)")
        verify_ip(request)
        return
    
    raise HTTPException(status_code=403, detail="Invalid or missing token")