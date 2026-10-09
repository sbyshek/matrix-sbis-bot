from fastapi import Request, HTTPException, Header
from app.core.config import settings
from ipaddress import ip_address, ip_network
import logging

logger = logging.getLogger(__name__)


# ── Доверенные прокси (только от них читаем X-Forwarded-For) ──
_TRUSTED = [
    ip_network(s.strip())
    for s in (settings.TRUSTED_PROXY_SUBNETS or "172.16.0.0/12").split(",")
    if s.strip()
]

def get_client_ip(request: Request) -> str:
    """
    Реальный IP клиента.
    X-Forwarded-For читаем ТОЛЬКО если peer — доверенный прокси (nginx).
    Прямой запрос мимо прокси → возвращается peer (подделать XFF снаружи нельзя).
    """
    peer = request.client.host
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        try:
            if any(ip_address(peer) in net for net in _TRUSTED):
                return xff.split(",")[0].strip()
        except ValueError:
            pass
    return peer


def verify_local_token(x_local_token: str = Header(..., alias="X-Local-Token")):
    """Проверяет локальный API-токен для внутренних вызовов между сервисами"""
    if x_local_token != settings.LOCAL_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid local token")
    logger.info("✅ Local token verified")


def verify_ip(request: Request):
    """Проверяет, что запрос пришёл с разрешённого IP (с учётом прокси)"""
    client_ip = get_client_ip(request)
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
    if x_local_token and x_local_token == settings.LOCAL_API_TOKEN:
        logger.info("✅ UI access granted via X-Local-Token")
        verify_ip(request)
        return

    if x_api_token and x_api_token == settings.DASHBOARD_API_TOKEN:
        logger.info("✅ UI access granted via X-API-Token (dashboard)")
        verify_ip(request)
        return