import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request

from app.alert_svc import TEMPLATES, TEMPLATES_FILE, get_location_alerts, get_template_by_id
from app.core.config import settings
from app.core.history import get_unified_history, save_unified_history
from app.core.matrix import send_alert_to_matrix, send_event_to_matrix
from app.core.redis import get_state, set_state
from app.core.security import verify_ip, verify_local_token
from app.core.voice import VoiceAPIError, get_campaign_logs, get_campaigns_list, trigger_campaign
from app.models import AlertTriggerRequest, AsteriskTriggerRequest, BulkAlertTriggerRequest
from app.security import LOCATIONS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["api"])


def require_dispatcher_token(
    request: Request,
    x_local_token: str | None,
    x_api_token: str | None
) -> None:
    token = x_local_token or x_api_token

    if not token:
        raise HTTPException(status_code=403, detail="Token required")

    if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    verify_ip(request)


# =============================================================================
# Templates
# =============================================================================

@router.get("/api/v1/templates")
async def get_templates(
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    require_dispatcher_token(request, x_local_token, x_api_token)

    templates_list = [
        {"id": tpl_id, **tpl_data}
        for tpl_id, tpl_data in TEMPLATES.items()
    ]

    return {"templates": templates_list}


@router.post("/api/v1/templates")
async def create_or_update_template(
    template_data: dict,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    request: Request = None
):
    verify_local_token(x_local_token)
    verify_ip(request)

    template_id = template_data.get("id")

    if not template_id:
        raise HTTPException(status_code=400, detail="Template ID is required")

    TEMPLATES[template_id] = {
        "name": template_data.get("name", ""),
        "audio_file": template_data.get("audio_file", ""),
        "severity": template_data.get("severity", "attention"),
        "default_text": template_data.get("default_text", "")
    }

    try:
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(TEMPLATES, f, ensure_ascii=False, indent=2)

        logger.info(f"✅ Template saved: {template_id}")

        return {
            "status": "success",
            "template_id": template_id
        }

    except Exception as e:
        logger.error(f"❌ Failed to save template: {e}")
        raise HTTPException(status_code=500, detail="Failed to save template")


@router.delete("/api/v1/templates/{template_id}")
async def delete_template(
    template_id: str,
    x_local_token: str = Header(..., alias="X-Local-Token"),
    request: Request = None
):
    verify_local_token(x_local_token)
    verify_ip(request)

    if template_id not in TEMPLATES:
        raise HTTPException(status_code=404, detail="Template not found")

    del TEMPLATES[template_id]

    try:
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(TEMPLATES, f, ensure_ascii=False, indent=2)

        logger.info(f"🗑️ Template deleted: {template_id}")

        return {"status": "success"}

    except Exception as e:
        logger.error(f"❌ Failed to delete template: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete template")


# =============================================================================
# Alert trigger
# =============================================================================

@router.post("/api/v1/alert/trigger")
async def trigger_alert_template(
    request_data: AlertTriggerRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    require_dispatcher_token(request, x_local_token, x_api_token)

    loc_id = request_data.loc_id

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail=f"Location '{loc_id}' not found")

    loc_data = LOCATIONS[loc_id]

    template = get_template_by_id(request_data.template_id)

    if not template:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{request_data.template_id}' not found"
        )

    alert_config = next(
        (
            alert for alert in get_location_alerts(loc_id)
            if alert["template_id"] == template["id"]
        ),
        None
    )

    if not alert_config:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template['name']}' not configured for location '{loc_data['name']}'"
        )

    subscribers = alert_config.get("subscribers", [])

    if not subscribers:
        raise HTTPException(
            status_code=400,
            detail="No subscribers configured for this alert"
        )

    try:
        result = await trigger_campaign(
            loc_id=loc_id,
            loc_name=loc_data["name"],
            node_id=loc_data.get("asterisk_node", "zv-asterisk"),
            template=template,
            alert_config=alert_config,
            subscribers=subscribers,
            initiator="dashboard_user"
        )
    except VoiceAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    campaign_id = result.get("campaign_id")

    await send_alert_to_matrix(
        loc_id=loc_id,
        template_id=template["id"],
        author_user_id="dashboard_user"
    )

    await save_unified_history(
        loc_id=loc_id,
        loc_name=loc_data["name"],
        category="campaign",
        action="triggered",
        severity=template.get("severity", "attention"),
        title=template.get("name", template["id"]),
        description=alert_config.get("text_for_matrix") or template.get("default_text", ""),
        source="dashboard_user",
        template_id=template["id"],
        template_name=template.get("name", template["id"]),
        campaign_id=campaign_id,
        subscribers_count=len(subscribers)
    )

    logger.info(f"✅ Alert triggered: {template['name']} for {loc_data['name']}")

    return {
        "status": "success",
        "template": template["name"],
        "location": loc_data["name"],
        "subscribers_count": len(subscribers),
        "campaign_id": campaign_id,
        "voice_api_response": result
    }


# =============================================================================
# Bulk alert
# =============================================================================

@router.post("/api/v1/alert/trigger-bulk")
async def trigger_bulk_alert(
    request_data: BulkAlertTriggerRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    require_dispatcher_token(request, x_local_token, x_api_token)

    template = get_template_by_id(request_data.template_id)

    if not template:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{request_data.template_id}' not found"
        )

    results = {
        "success": [],
        "failed": [],
        "total": len(request_data.loc_ids)
    }

    async def trigger_single(loc_id: str):
        try:
            if loc_id not in LOCATIONS:
                return {
                    "loc_id": loc_id,
                    "error": f"Location '{loc_id}' not found"
                }

            loc_data = LOCATIONS[loc_id]

            alert_config = next(
                (
                    alert for alert in get_location_alerts(loc_id)
                    if alert["template_id"] == template["id"]
                ),
                None
            )

            if not alert_config:
                return {
                    "loc_id": loc_id,
                    "error": f"Template not configured for '{loc_id}'"
                }

            subscribers = alert_config.get("subscribers", [])

            if not subscribers:
                return {
                    "loc_id": loc_id,
                    "error": "No subscribers"
                }

            result = await trigger_campaign(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                node_id=loc_data.get("asterisk_node", "zv-asterisk"),
                template=template,
                alert_config=alert_config,
                subscribers=subscribers,
                initiator="bulk_dispatcher"
            )

            campaign_id = result.get("campaign_id")

            await send_alert_to_matrix(
                loc_id=loc_id,
                template_id=template["id"],
                author_user_id="bulk_dispatcher"
            )

            await save_unified_history(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                category="campaign",
                action="triggered",
                severity=template.get("severity", "attention"),
                title=template.get("name", template["id"]),
                description=alert_config.get("text_for_matrix") or template.get("default_text", ""),
                source="bulk_dispatcher",
                template_id=template["id"],
                template_name=template.get("name", template["id"]),
                campaign_id=campaign_id,
                subscribers_count=len(subscribers)
            )

            return {
                "loc_id": loc_id,
                "loc_name": loc_data["name"],
                "campaign_id": campaign_id,
                "subscribers_count": len(subscribers)
            }

        except VoiceAPIError as e:
            return {
                "loc_id": loc_id,
                "error": e.detail
            }

        except Exception as e:
            return {
                "loc_id": loc_id,
                "error": str(e)
            }

    tasks = [
        trigger_single(loc_id)
        for loc_id in request_data.loc_ids
    ]

    campaign_results = await asyncio.gather(*tasks)

    for result in campaign_results:
        if "error" in result:
            results["failed"].append(result)
        else:
            results["success"].append(result)

    logger.info(
        f"✅ Bulk alert: {template['name']} on "
        f"{len(results['success'])}/{len(request_data.loc_ids)} locations"
    )

    return results


# =============================================================================
# Campaign details / campaigns history
# =============================================================================

@router.get("/api/v1/campaign/{campaign_id}/details")
async def get_campaign_details(
    campaign_id: str,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    require_dispatcher_token(request, x_local_token, x_api_token)

    try:
        return await get_campaign_logs(campaign_id)
    except VoiceAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/api/v1/campaigns/history")
async def get_campaigns_history(
    limit: int = 50,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    require_dispatcher_token(request, x_local_token, x_api_token)

    try:
        return await get_campaigns_list(limit=limit)
    except VoiceAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# =============================================================================
# Unified history
# =============================================================================

@router.get("/api/v1/history")
async def get_history(
    limit: int = 100,
    loc_id: str = None,
    category: str = None,
    action: str = None,
    template_id: str = None,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    require_dispatcher_token(request, x_local_token, x_api_token)

    history = await get_unified_history(
        limit=limit,
        loc_id=loc_id,
        category=category,
        action=action,
        template_id=template_id
    )

    return {
        "history": history,
        "count": len(history),
        "limit": limit
    }
    
    
@router.get("/api/v1/alerts/history")
async def get_alerts_history(
    limit: int = 100,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None
):
    require_dispatcher_token(request, x_local_token, x_api_token)
    
    try:
        return await get_campaigns_list(limit=limit)
    except VoiceAPIError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# =============================================================================
# Asterisk voice incident
# =============================================================================

@router.post("/api/v1/incident/asterisk")
async def trigger_from_asterisk_voice(
    request_data: AsteriskTriggerRequest,
    x_api_token: str = Header(..., alias="X-API-Token")
):
    if x_api_token != settings.ASTERISK_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid X-API-Token")

    loc_id = request_data.loc_id

    if loc_id not in LOCATIONS:
        raise HTTPException(status_code=404, detail=f"Location {loc_id} not found")

    loc_data = LOCATIONS[loc_id]

    event_id = str(uuid.uuid4())[:8]

    new_event = {
        "id": event_id,
        "type": "alarm",
        "comment": f"🎙️ [Голос от {request_data.caller_id}]: {request_data.comment}",
        "timestamp": datetime.now().isoformat(),
        "source": request_data.source
    }

    state = await get_state(loc_id)

    state.setdefault("events", []).append(new_event)
    state["updated_at"] = datetime.now().isoformat()

    await set_state(loc_id, state)

    await save_unified_history(
        loc_id=loc_id,
        loc_name=loc_data["name"],
        category="incident",
        action="created",
        severity="alarm",
        title="Голосовой инцидент",
        description=new_event["comment"],
        source=request_data.source,
        event_id=event_id
    )

    await send_event_to_matrix(
        loc_id=loc_id,
        event_type="alarm",
        comment=new_event["comment"],
        author_user_id="Asterisk Voice Bot"
    )

    logger.info(f"✅ Incident created from voice: {loc_id}, event_id: {event_id}")

    return {
        "status": "success",
        "event_id": event_id
    }