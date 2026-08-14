import asyncio
import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Request

from app.alert_svc import TEMPLATES, TEMPLATES_FILE, get_location_alerts, get_template_by_id
from app.core.config import settings
from app.core.history import get_unified_history, save_unified_history
from app.core.matrix import send_alert_to_matrix, send_event_to_matrix, send_matrix_message
from app.core.redis import get_state, set_state, save_campaign_history, get_campaign_history, get_redis
from app.core.security import verify_ip, verify_local_token
from app.core.voice import VoiceAPIError, get_campaign_logs, get_campaigns_list, trigger_campaign
from app.models import AlertTriggerRequest, AsteriskTriggerRequest, BulkAlertTriggerRequest, CustomAlertTriggerRequest, AlertTriggerByCodeRequest
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
        
        await send_alert_to_matrix(
            loc_id=loc_id,
            template_id=template["id"],
            author_user_id="dashboard_user"
        )
        
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

    if campaign_id:
        try:
            save_campaign_history(campaign_id, result.get("result",{}))
        except Exception as e:
            logger.error(f"❌ Failed to save campaign history: {e}")
    
    # await send_alert_to_matrix(
    #     loc_id=loc_id,
    #     template_id=template["id"],
    #     author_user_id="dashboard_user"
    # )

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

            await send_alert_to_matrix(
                loc_id=loc_id,
                template_id=template["id"],
                author_user_id="bulk_dispatcher"
            )
            
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

            if campaign_id:
                try:
                    save_campaign_history(campaign_id, result.get("result",{}))
                except Exception as e:
                    logger.error(f"❌ Failed to save campaign history: {e}")
            
            # await send_alert_to_matrix(
            #     loc_id=loc_id,
            #     template_id=template["id"],
            #     author_user_id="bulk_dispatcher"
            # )

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
        # return await get_campaign_logs(campaign_id)
        data = await get_campaign_logs(campaign_id)
        launch_raw = await get_campaign_history(campaign_id)
        if launch_raw:
            try:
                
                data['launch_details'] = (
                    json.loads(launch_raw)
                    if isinstance(launch_raw, (str, bytes, bytearray)) 
                    else launch_raw
                )
                
            except Exception as e:
                logger.error(f"❌ Failed to parse launch details: {e}")
                data['launch_details'] = launch_raw

        return data
        
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
    
    verify_local_token(x_api_token)
    # if x_api_token != settings.ASTERISK_API_TOKEN:
    #     raise HTTPException(status_code=403, detail="Invalid X-API-Token")

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
    
    


@router.post("/api/v1/alert/trigger-custom")
async def trigger_custom_alert(
    request_data: CustomAlertTriggerRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    x_api_token: str = Header(default=None, alias="X-API-Token")
):
    """
    🎙️ Тестовое оповещение произвольной записью.
    Абоненты — объединённый уникальный список оповещения локации.
    """

    require_dispatcher_token(request, x_local_token, x_api_token)

    results = {"success": [], "failed": [], "total": len(request_data.loc_ids)}

    async def trigger_single(loc_id: str):
        try:
            if loc_id not in LOCATIONS:
                return {"loc_id": loc_id, "error": "Location not found"}

            loc_data = LOCATIONS[loc_id]

            # Уникальные абоненты по ВСЕМ сценариям локации
            seen = {}
            for alert in get_location_alerts(loc_id):
                for raw in alert.get("subscribers", []):
                    key = str(raw).split(":", 1)[0].strip()
                    if key and key not in seen:
                        seen[key] = raw
            subscribers = list(seen.values())

            if not subscribers:
                return {"loc_id": loc_id, "error": "No subscribers"}

            text = request_data.text_for_matrix or "🔔 Тестовое голосовое оповещение"

            if request_data.text_for_matrix:
                room_id = loc_data.get("room_id")
                if room_id:
                    tz = ZoneInfo(settings.TIMEZONE)
                    time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")
                    msg = (
                        f"🎙️ **{loc_data['name']}: ТЕСТОВОЕ ОПОВЕЩЕНИЕ**\n"
                        f"{request_data.text_for_matrix}\n"
                        f"Время: {time_str}"
                    )
                    await send_matrix_message(room_id, msg)

        
            
            # 🔥 Через voice-модуль — как все остальные эндпоинты (httpx не нужен)
            result = await trigger_campaign(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                node_id=loc_data.get("asterisk_node", "zv-asterisk"),
                template={
                    "id": "custom_test",
                    "name": "Тестовое оповещение",
                    "severity": "attention",
                    "audio_file": request_data.audio_file,
                    "default_text": text
                },
                alert_config={
                    "audio_file": request_data.audio_file,
                    "text_for_matrix": text
                },
                subscribers=subscribers,
                initiator="custom_test"
            )

            campaign_id = result.get("campaign_id")

            # Детали запуска — в Redis (для модалки результатов)
            if campaign_id:
                try:
                    await get_redis().set(
                        f"dispatch:campaign_launch:{campaign_id}",
                        json.dumps(result.get("results", {}), ensure_ascii=False),
                        ex=settings.HISTORY_TTL_HOURS * 3600
                    )
                except Exception as e:
                    logger.error(f"❌ Failed to store launch details: {e}")

            # Единая история
            await save_unified_history(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                category="campaign",
                action="triggered",
                severity="attention",
                title="🎙️ Тестовое оповещение",
                description=text,
                source="custom_test",
                template_id="custom_test",
                template_name="Тестовое оповещение",
                campaign_id=campaign_id,
                subscribers_count=len(subscribers)
            )

            # Опционально — сообщение в Matrix комнату локации
            # if request_data.text_for_matrix:
            #     room_id = loc_data.get("room_id")
            #     if room_id:
            #         tz = ZoneInfo(settings.TIMEZONE)
            #         time_str = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")
            #         msg = (
            #             f"🎙️ **{loc_data['name']}: ТЕСТОВОЕ ОПОВЕЩЕНИЕ**\n"
            #             f"{request_data.text_for_matrix}\n"
            #             f"Время: {time_str}"
            #         )
            #         await send_matrix_message(room_id, msg)

            return {
                "loc_id": loc_id,
                "loc_name": loc_data["name"],
                "campaign_id": campaign_id,
                "subscribers_count": len(subscribers)
            }

        except VoiceAPIError as e:
            logger.error(f"❌ Custom trigger VoiceAPIError for {loc_id}: {e.detail}")
            return {"loc_id": loc_id, "error": e.detail}
        except Exception as e:
            # 🔥 Теперь любая ошибка видна в логах
            logger.error(f"❌ Custom trigger failed for {loc_id}: {e}")
            return {"loc_id": loc_id, "error": str(e)}

    tasks = [trigger_single(loc_id) for loc_id in request_data.loc_ids]
    for r in await asyncio.gather(*tasks):
        (results["failed"] if "error" in r else results["success"]).append(r)

    logger.info(f"✅ Custom alert: {len(results['success'])}/{len(request_data.loc_ids)} locations")
    return results


@router.post("/api/v1/alert/trigger-by-code")
async def trigger_alert_by_code(
    request_data: AlertTriggerByCodeRequest,
    request: Request,
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    
):
    """
    📞 IVR-запуск оповещения по коду с телефона.
    Мягко отрабатывает дубли кодов:
    1 совпадение — запуск; N — разбор по номеру звонящего, иначе все + warning.
    """
    
    verify_local_token(x_local_token)

    code = request_data.code.strip()
    caller = request_data.caller.strip()
    caller_digits = "".join(ch for ch in caller if ch.isdigit())

    # 1. Все совпадения по всем локациям
    matches = []
    for loc_id in LOCATIONS.keys():
        for alert in get_location_alerts(loc_id):
            if code and str(alert.get("trigger_code", "")).strip() == code:
                matches.append({"loc_id": loc_id, "alert": alert})

    if not matches:
        logger.warning(f"❌ IVR: code '{code}' from {caller} not found")
        raise HTTPException(status_code=404, detail="Code not found")

    ambiguous = len(matches) > 1

    if ambiguous:
        # 2. Мягкий разбор по номеру звонящего
        preferred = [
            m for m in matches
            if any(
                str(s).split(":", 1)[0].strip() == caller_digits
                for s in m["alert"].get("subscribers", [])
            )
        ]
        if preferred:
            logger.warning(
                f"⚠️ IVR: duplicate code '{code}' ({len(matches)} matches) "
                f"resolved by caller {caller} -> {[m['loc_id'] for m in preferred]}"
            )
            matches = preferred
        else:
            logger.error(
                f"🚨 IVR: duplicate code '{code}' ({len(matches)} matches), "
                f"caller {caller} not in subscribers -> triggering ALL matches"
            )

    # 3. Запуск всех выбранных совпадений параллельно
    async def fire(m: dict):
        loc_id = m["loc_id"]
        alert = m["alert"]
        loc_data = LOCATIONS[loc_id]
        template = get_template_by_id(alert["template_id"]) or {
            "id": alert["template_id"],
            "name": alert["template_id"],
            "severity": "attention",
            "audio_file": "custom/test-message-8000",
            "default_text": ""
        }
        subscribers = alert.get("subscribers", [])
        if not subscribers:
            return {"loc_id": loc_id, "error": "No subscribers"}

        try:
            result = await trigger_campaign(
                loc_id=loc_id,
                loc_name=loc_data["name"],
                node_id=loc_data.get("asterisk_node", "zv-asterisk"),
                template=template,
                alert_config=alert,
                subscribers=subscribers,
                initiator=f"ivr_{caller}"
            )
        except VoiceAPIError as e:
            return {"loc_id": loc_id, "error": e.detail}

        # Matrix + единая история
        await send_alert_to_matrix(loc_id, alert["template_id"], f"ivr_{caller}")
        await save_unified_history(
            loc_id=loc_id,
            loc_name=loc_data["name"],
            category="campaign",
            action="triggered",
            severity=template.get("severity", "attention"),
            title=f"📞 IVR {code}: {template.get('name', '')}",
            description=alert.get("text_for_matrix", ""),
            source=f"ivr_{caller}",
            template_id=template["id"],
            template_name=template.get("name", template["id"]),
            campaign_id=result.get("campaign_id"),
            subscribers_count=len(subscribers)
        )
        return {
            "loc_id": loc_id,
            "loc_name": loc_data["name"],
            "template_id": template["id"],
            "campaign_id": result.get("campaign_id")
        }

    fired = await asyncio.gather(*[fire(m) for m in matches])

    success = [r for r in fired if "error" not in r]
    failed = [r for r in fired if "error" in r]

    logger.info(f"✅ IVR code '{code}' by {caller}: {len(success)} triggered, {len(failed)} failed, ambiguous={ambiguous}")

    return {
        "status": "success",
        "code": code,
        "caller": caller,
        "ambiguous": ambiguous,
        "matched": [
            {
                "loc_id": m["loc_id"],
                "loc_name": LOCATIONS[m["loc_id"]]["name"],
                "template_id": m["alert"]["template_id"]
            }
            for m in matches
        ],
        "success": success,
        "failed": failed
    }
    
@router.get("/api/v1/ivr/codes")
async def list_ivr_codes(
    x_local_token: str = Header(default=None, alias="X-Local-Token"),
    # x_api_token: str = Header(default=None, alias="X-API-Token"),
    request: Request = None,
):
    """Список всех IVR-кодов с подсветкой дублей."""
    # token = x_local_token or x_api_token
    # if not token:
    #     raise HTTPException(status_code=403, detail="Token required")
    # if token != settings.LOCAL_API_TOKEN and token != settings.DASHBOARD_API_TOKEN:
    #     raise HTTPException(status_code=403, detail="Invalid token")

    verify_local_token(x_local_token=x_local_token)

    seen = {}
    for loc_id in LOCATIONS.keys():
        for alert in get_location_alerts(loc_id):
            c = str(alert.get("trigger_code", "")).strip()
            if c:
                seen.setdefault(c, []).append({
                    "loc_id": loc_id,
                    "loc_name": LOCATIONS[loc_id]["name"],
                    "template_id": alert["template_id"],
                    "template_name": alert.get("name")
                })

    codes = [
        {"code": c, "duplicate": len(v) > 1, "entries": v}
        for c, v in sorted(seen.items())
    ]

    return {
        "codes": codes,
        "duplicates": [x for x in codes if x["duplicate"]]
    }