import httpx
import asyncio
import logging
from datetime import datetime
from app.core.config import settings # Импортируйте ваши настройки
from app.core.matrix import send_matrix_message # Ваша существующая функция

logger = logging.getLogger(__name__)

async def check_trunks_and_alert():
    """Проверяет здоровье транков и отправляет алерт в Matrix при проблемах."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.VOICE_API_URL}/api/v1/trunks/health",
                headers={"X-Secret": settings.VOICE_API_SECRET}
            )
            response.raise_for_status()
            data = response.json()
            
            bad_trunks = []
            for node in data.get("nodes", []):
                node_name = node.get("name", "Unknown Node")
                for trunk in node.get("trunks", []):
                    # Условия для алерта: статус offline/unknown или регистрация offline
                    if trunk.get("status") in ("offline", "unknown") or trunk.get("registration") == "offline":
                        bad_trunks.append(
                            f"🔹 **Узел:** `{node_name}`\n"
                            f"🔹 **Транк:** `{trunk['name']}`\n"
                            f"🔹 **Статус:** `{trunk['status']}` (регистрация: `{trunk['registration']}`)\n"
                            f"🔹 **Детали:** `{trunk['raw']}`\n"
                        )
            
            if bad_trunks:
                # Формируем красивое Markdown-сообщение
                message = (
                    f"⚠️ **ВНИМАНИЕ: Проблемы с телефонией!**\n"
                    f"🕒 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    + "\n---\n".join(bad_trunks)
                )
                
                # Отправляем в Matrix (замените на ваш ID комнаты для алертов)
                success = await send_matrix_message(settings.DASHBOARD_MATRIX_ROOM_ID, message)
                if success:
                    logger.warning(f"📤 Алерт о проблемах с транками отправлен в Matrix")
                else:
                    logger.error("❌ Не удалось отправить алерт в Matrix")
            else:
                logger.debug("✅ Проверка транков: все в норме")
                
    except httpx.HTTPStatusError as e:
        logger.error(f"❌ Ошибка HTTP при проверке транков: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при проверке транков: {e}")


async def periodic_trunk_monitor():
    """Фоновая задача, запускающая проверку каждые N минут."""
    check_interval = 300  # 5 минут (в секундах)
    logger.info(f"🔄 Запущен фоновый мониторинг транков (интервал: {check_interval} сек)")
    
    while True:
        await check_trunks_and_alert()
        await asyncio.sleep(check_interval)