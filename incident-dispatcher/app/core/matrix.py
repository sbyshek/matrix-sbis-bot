import logging
import uuid
from datetime import datetime

import httpx
import markdown
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.security import LOCATIONS
from app.alert_svc import TEMPLATES

logger = logging.getLogger(__name__)


def normalize_message(text: str) -> str:
    """
    Приводит сообщение к нормальному виду перед отправкой в Matrix.

    Важно:
    - r"\\n" / r"\n" — заменяет литерал \n на настоящий перевод строки;
    - "\r\n" — нормализует windows-переводы строк.
    """
    text = str(text or "")

    # Если из .env / JSON пришло как текст: \n
    text = text.replace(r"\n", "\n")

    # Если вдруг есть CRLF
    text = text.replace("\r\n", "\n")

    return text.strip()


async def send_matrix_message(room_id: str, message: str) -> bool:
    """
    Низкоуровневая отправка сообщения в Matrix.
    """
    if not room_id:
        logger.error("❌ Matrix: room_id is empty")
        return False

    message = normalize_message(message) or ""

    html_body = markdown.markdown(message, extensions=["nl2br"])

    payload = {
        "msgtype": "m.text",
        "body": message,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body
    }

    txn_id = f"msg_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:4]}"

    url = (
        f"{settings.MATRIX_HOMESERVER}"
        f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.MATRIX_ACCESS_TOKEN}"
                }
            )

            if response.status_code in (200, 201):
                logger.info(f"✅ Matrix message sent to room {room_id}")
                return True

            logger.error(
                f"❌ Matrix API error {response.status_code}: {response.text}"
            )
            return False

    except Exception as e:
        logger.error(f"❌ Matrix connection error: {e}")
        return False


def _now_local_str() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("Asia/Novosibirsk")

    return datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")


def build_event_message(
    loc_id: str,
    event_type: str,
    comment: str,
    author_user_id: str
) -> str:
    """
    Собирает сообщение для событий:
    normal / attention / alarm
    """
    loc = LOCATIONS.get(loc_id, {})
    loc_name = loc.get("name", loc_id)

    if event_type == "attention":
        template = settings.TEMPLATE_ATTENTION
    elif event_type == "alarm":
        template = settings.TEMPLATE_ALARM
    else:
        template = settings.TEMPLATE_NORMAL

    message = template.replace("{location}", loc_name)
    message = message.replace("{comment}", comment or "Без комментария")
    message = message.replace("{time}", _now_local_str())
    message = message.replace("{author}", author_user_id or "system")

    return message


async def send_event_to_matrix(
    loc_id: str,
    event_type: str,
    comment: str,
    author_user_id: str
) -> bool:
    """
    Отправляет событие диспетчера:
    normal / attention / alarm
    """
    loc = LOCATIONS.get(loc_id)

    if not loc:
        logger.error(f"❌ Matrix: location {loc_id} not found")
        return False

    room_id = loc.get("room_id")

    if not room_id:
        logger.error(f"❌ Matrix: location {loc_id} has no room_id")
        return False

    message = build_event_message(
        loc_id=loc_id,
        event_type=event_type,
        comment=comment,
        author_user_id=author_user_id
    )

    return await send_matrix_message(room_id, message)


async def send_alert_to_matrix(
    loc_id: str,
    template_id: str,
    author_user_id: str
) -> bool:
    """
    Отправляет сообщение о запуске сценария оповещения.
    """
    loc = LOCATIONS.get(loc_id)

    if not loc:
        logger.error(f"❌ Matrix: location {loc_id} not found")
        return False

    room_id = loc.get("room_id")

    if not room_id:
        logger.error(f"❌ Matrix: location {loc_id} has no room_id")
        return False

    alert_template = next(
        (
            alert for alert in loc.get("alerts", [])
            if alert.get("template_id") == template_id
        ),
        None
    )

    if alert_template:
        message = alert_template.get("matrix_text", "")
    else:
        message = ""

    if not message:
        global_template = TEMPLATES.get(template_id, {})
        message = global_template.get("default_text", "")

    if not message:
        logger.error(
            f"❌ Matrix: message is empty for {loc_id}/{template_id}"
        )
        return False

    message = message.replace("{time}", _now_local_str())
    message = message.replace("{author}", author_user_id or "система")

    return await send_matrix_message(room_id, message)