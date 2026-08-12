import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.alert_svc import get_location_alerts
from app.core.config import settings
from app.core.redis import get_state
from app.core.security import verify_ip
from app.security import LOCATIONS, verify_dashboard_access

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

templates = Jinja2Templates(directory="templates")


def get_effective_mode(events: list) -> str:
    if not events:
        return "normal"

    if any(e.get("type") == "alarm" for e in events):
        return "alarm"

    if any(e.get("type") == "attention" for e in events):
        return "attention"

    return "normal"


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "dashboard_token": settings.DASHBOARD_API_TOKEN
        }
    )


@router.get("/api/dashboard")
async def get_dashboard_state(
    request: Request,
    _=Depends(verify_dashboard_access)
):
    result = {}

    for loc_id, loc_data in LOCATIONS.items():
        state = await get_state(loc_id)

        result[loc_id] = {
            "name": loc_data["name"],
            "icon": loc_data.get("icon", "📍"),
            "mode": get_effective_mode(state.get("events", [])),
            "events": state.get("events", []),
            "updated_at": state.get("updated_at", ""),
            "alerts": get_location_alerts(loc_id),
            "allowed_ips": loc_data.get("allowed_ips", [])
        }

    return result


@router.get("/api/v1/locations")
async def get_locations(
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    token = x_local_token or x_api_token

    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)

    locations = []

    for loc_id, loc_config in LOCATIONS.items():
        locations.append({
            "id": loc_id,
            "name": loc_config["name"],
            "icon": loc_config.get("icon", "📍"),
            "alerts": get_location_alerts(loc_id),
            "allowed_ips": loc_config.get("allowed_ips", [])
        })

    return {"locations": locations}