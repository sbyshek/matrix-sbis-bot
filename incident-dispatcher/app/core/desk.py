import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.alert_svc import TEMPLATES, get_location_alerts
from app.core.config import settings
from app.core.history import get_location_unified_history, save_unified_history
from app.core.matrix import send_alert_to_matrix
from app.core.redis import get_state
from app.core.voice import VoiceAPIError, get_campaign_logs, trigger_campaign, get_campaigns_list
from app.security import LOCATIONS, check_ip_whitelist

logger = logging.getLogger(__name__)

router = APIRouter(tags=["desk"])

templates = Jinja2Templates(directory="templates")


def get_effective_mode(events: list) -> str:
    if not events:
        return "normal"

    if any(e.get("type") == "alarm" for e in events):
        return "alarm"

    if any(e.get("type") == "attention" for e in events):
        return "attention"

    return "normal"


def verify_desk_access(
    request: Request,
    loc_id: str,
    x_api_token: Optional[str] = None
) -> None:
    loc = LOCATIONS.get(loc_id)

    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    # Опционально: глобальный dashboard token + dashboard IP
    if x_api_token and x_api_token == settings.DASHBOARD_API_TOKEN:
        dashboard_ips = [
            ip.strip()
            for ip in settings.DASHBOARD_ALLOWED_IPS.split(",")
            if ip.strip()
        ]

        if check_ip_whitelist(request, dashboard_ips):
            logger.info(f"✅ Desk access granted via dashboard token for {loc_id}")
            return

    allowed_ips = loc.get("allowed_ips", [])

    if allowed_ips and check_ip_whitelist(request, allowed_ips):
        logger.info(f"✅ Desk access granted by allowed_ips for {loc_id}")
        return

    raise HTTPException(
        status_code=403,
        detail="Access denied: IP not allowed for this location desk"
    )


@router.get("/desk/{loc_id}", response_class=HTMLResponse)
async def location_desk_page(request: Request, loc_id: str):
    try:
        verify_desk_access(request, loc_id)
    except HTTPException:
        return templates.TemplateResponse(
            "403.html",
            {
                "request": request,
                "message": f"Доступ к пульту локации '{loc_id}' разрешён только с разрешённых IP."
            },
            status_code=403
        )

    loc = LOCATIONS[loc_id]

    return templates.TemplateResponse(
        "location_dashboard.html",
        {
            "request": request,
            "loc_id": loc_id,
            "loc_name": loc["name"],
            "loc_icon": loc.get("icon", "📍")
        }
    )


@router.get("/api/v1/desk/{loc_id}/state")
async def get_desk_state(
    loc_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    verify_desk_access(request, loc_id, x_api_token)

    loc = LOCATIONS[loc_id]
    state = await get_state(loc_id)

    return {
        "loc_id": loc_id,
        "name": loc["name"],
        "icon": loc.get("icon", "📍"),
        "mode": get_effective_mode(state.get("events", [])),
        "events": state.get("events", []),
        "updated_at": state.get("updated_at", ""),
        "alerts": get_location_alerts(loc_id)
    }


@router.get("/api/v1/desk/{loc_id}/templates")
async def get_desk_templates(
    loc_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    verify_desk_access(request, loc_id, x_api_token)

    alerts = get_location_alerts(loc_id)
    templates_list = []

    for alert in alerts:
        global_template = TEMPLATES.get(alert["template_id"], {})

        templates_list.append({
            "template_id": alert["template_id"],
            "name": alert.get("name", alert["template_id"]),
            "severity": alert.get("severity", "attention"),
            "audio_file": alert.get("audio_file", ""),
            "default_text": global_template.get("default_text", ""),
            "matrix_text": alert.get("text_for_matrix", ""),
            "trigger_code": alert.get("trigger_code", ""),
            "subscribers": alert.get("subscribers", []),
            "subscribers_count": len(alert.get("subscribers", []))
        })

    return {"templates": templates_list}


@router.get("/api/v1/desk/{loc_id}/history")
async def get_desk_history(
    loc_id: str,
    limit: int = 100,
    category: Optional[str] = None,
    action: Optional[str] = None,
    template_id: Optional[str] = None,
    request: Request = None,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    verify_desk_access(request, loc_id, x_api_token)

    history = await get_location_unified_history(
        loc_id,
        limit=limit,
        category=category,
        action=action,
        template_id=template_id
    )

    return {
        "loc_id": loc_id,
        "history": history,
        "count": len(history),
        "limit": limit
    }


@router.post("/api/v1/desk/{loc_id}/alert/trigger")
async def desk_trigger_alert(
    loc_id: str,
    payload: dict,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    verify_desk_access(request, loc_id, x_api_token)

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")

    loc_data = LOCATIONS[loc_id]

    template_id = payload.get("template_id")

    if not template_id:
        raise HTTPException(status_code=400, detail="template_id is required")

    template = TEMPLATES.get(template_id)

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    template = {"id": template_id, **template}

    alert_config = next(
        (
            alert for alert in get_location_alerts(loc_id)
            if alert["template_id"] == template_id
        ),
        None
    )

    if not alert_config:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_id}' not configured for '{loc_id}'"
        )

    subscribers = alert_config.get("subscribers", [])

    if not subscribers:
        raise HTTPException(status_code=400, detail="No subscribers configured")

    try:
        result = await trigger_campaign(
            loc_id=loc_id,
            loc_name=loc_data["name"],
            node_id=loc_data.get("asterisk_node", "zv-asterisk"),
            template=template,
            alert_config=alert_config,
            subscribers=subscribers,
            initiator=f"local_desk_{loc_id}"
        )
    except VoiceAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    campaign_id = result.get("campaign_id")

    await send_alert_to_matrix(
        loc_id=loc_id,
        template_id=template_id,
        author_user_id=f"local_desk_{loc_id}"
    )

    await save_unified_history(
        loc_id=loc_id,
        loc_name=loc_data["name"],
        category="campaign",
        action="triggered",
        severity=template.get("severity", "attention"),
        title=template.get("name", template_id),
        description=alert_config.get("text_for_matrix") or template.get("default_text", ""),
        source=f"local_desk_{loc_id}",
        template_id=template_id,
        template_name=template.get("name", template_id),
        campaign_id=campaign_id,
        subscribers_count=len(subscribers)
    )

    return {
        "status": "success",
        "template": template.get("name", template_id),
        "location": loc_data["name"],
        "subscribers_count": len(subscribers),
        "campaign_id": campaign_id,
        "voice_api_response": result
    }


@router.get("/api/v1/desk/{loc_id}/campaign/{campaign_id}/details")
async def get_desk_campaign_details(
    loc_id: str,
    campaign_id: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")
):
    verify_desk_access(request, loc_id, x_api_token)

    try:
        data = await get_campaign_logs(campaign_id)
    except VoiceAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    events = data.get("events", [])

    belongs_to_location = any(
        event.get("loc_id") == loc_id
        for event in events
    )

    if not belongs_to_location:
        raise HTTPException(
            status_code=403,
            detail="Campaign does not belong to this location"
        )

    return data