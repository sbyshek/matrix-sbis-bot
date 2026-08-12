import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class VoiceAPIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def trigger_campaign(
    *,
    loc_id: str,
    loc_name: str,
    node_id: str,
    template: dict,
    alert_config: dict,
    subscribers: list[str],
    initiator: str
) -> dict[str, Any]:
    """
    Запуск голосовой кампании через voice-api.
    """
    payload = {
        "node_id": node_id,
        "template_id": template["id"],
        "audio_file": alert_config.get("audio_file") or template.get("audio_file", ""),
        "text_for_matrix": alert_config.get("text_for_matrix") or template.get("default_text", ""),
        "subscribers": subscribers,
        "loc_id": loc_id,
        "loc_name": loc_name,
        "initiator": initiator
    }

    try:
        async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
            response = await client.post(
                f"{settings.VOICE_API_URL}/api/v1/campaign/trigger-template",
                json=payload,
                headers={
                    "X-Secret": settings.VOICE_API_SECRET
                }
            )

            if response.status_code != 200:
                logger.error(
                    f"❌ voice-api returned {response.status_code}: {response.text}"
                )
                raise VoiceAPIError(
                    status_code=502,
                    detail=f"voice-api error: {response.text}"
                )

            return response.json()

    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise VoiceAPIError(
            status_code=503,
            detail=f"voice-api unavailable: {str(e)}"
        )


async def get_campaign_logs(campaign_id: str) -> dict[str, Any]:
    """
    Получение логов кампании из voice-api.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.ASTERISK_ORIGINATE_TIMEOUT) as client:
            response = await client.get(
                f"{settings.VOICE_API_URL}/api/v1/campaign/{campaign_id}/logs",
                headers={
                    "X-Secret": settings.VOICE_API_SECRET
                }
            )

            if response.status_code != 200:
                raise VoiceAPIError(
                    status_code=502,
                    detail="Failed to fetch campaign details from voice-api"
                )

            return response.json()

    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise VoiceAPIError(
            status_code=503,
            detail="voice-api unavailable"
        )


async def get_campaigns_list(limit: int = 50) -> dict[str, Any]:
    """
    Получение списка кампаний из voice-api.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.VOICE_API_URL}/api/v1/campaigns/list",
                params={"limit": limit},
                headers={
                    "X-Secret": settings.VOICE_API_SECRET
                }
            )

            if response.status_code != 200:
                raise VoiceAPIError(
                    status_code=502,
                    detail="voice-api campaigns list error"
                )

            return response.json()

    except httpx.RequestError as e:
        logger.error(f"❌ Failed to connect to voice-api: {e}")
        raise VoiceAPIError(
            status_code=503,
            detail="voice-api unavailable"
        )