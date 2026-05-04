import asyncio
import json
import logging
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from cursor_manager import load_cursor, save_cursor
from nio import AsyncClient, SyncResponse, LoginResponse
import config # Импорт настроек бота (токен, сервер)
from token_manager import load_token, save_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# API теперь отдает всё подряд
API_URL_ALL = "http://alert_api:8000/api/v1/stream/all"

class AlertBot:
    def __init__(self):
        self.client = AsyncClient(config.HOMESERVER, config.USERNAME)
        self.routes = {}
        self.mappings = {}
        self.default_room = None
        self.routes_path = "/app/config/routes.json"
        self.last_check = load_cursor() 
        # self.last_check = None # Глобальная метка времени
        
        if not self.last_check:
            self.last_check = datetime.now(timezone.utc).isoformat()
            save_cursor(self.last_check)  # Создаём файл с текущим временем
            logger.info("📝 Cursor file created from current time")
            
        self.load_routes()
        

    def _utc_to_local(self, utc_iso: str) -> str:
        """Конвертирует ISO-строку из UTC в настроенный часовой пояс"""
        try:
            dt_utc = datetime.fromisoformat(utc_iso)
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(ZoneInfo(config.TIMEZONE))
            return dt_local.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            # Фолбэк на обрезку строки, если конвертация не удалась
            return utc_iso[:19].replace("T", " ")
    
    def load_routes(self):
        """Читаем JSON-конфиг маршрутов"""
        try:
            with open("/app/config/routes.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                self.mappings = data.get("mappings", {})
                self.default_room = data.get("default_room")
                logger.info(f"️ Routes loaded: {len(self.mappings)} active rooms")
        except Exception as e:
            logger.error(f"❌ Failed to load routes.json: {e}")
            self.mappings = {}

    async def login(self):
        resp = await self.client.login(config.PASSWORD)
        if isinstance(resp, LoginResponse):
            logger.info("✅ AlertBot logged in")
            return True
        return False

    async def send(self, room: str, plain: str, html: str):
        try:
            # await self.client.room_send(room, "m.room.message", {"msgtype": "m.text", "body": text})
            await self.client.room_send(room, "m.room.message", {
            "msgtype": "m.text",
            "body": plain,  # Фолбэк для текстовых клиентов
            "format": "org.matrix.custom.html",
            "formatted_body": html  # Рендерится в Element/Web
        })
        except Exception as e:
            logger.error(f"Failed to send to {room}: {e}")

    def format_message(self, alert: dict) -> tuple[str, str]:
        # Маппинг severity -> (plain_текст, html_бейдж)
        badges = {
            "info": ("[INFO]", '<span style="background:#3498db; color:#fff; padding:2px 6px; border-radius:4px; font-weight:bold;">ℹ️ INFO</span>'),
            "warning": ("[WARNING]", '<span style="background:#f39c12; color:#fff; padding:2px 6px; border-radius:4px; font-weight:bold;">⚠️ WARNING</span>'),
            "critical": ("[CRITICAL]", '<span style="background:#e74c3c; color:#fff; padding:2px 6px; border-radius:4px; font-weight:bold;">🚨 CRITICAL</span>'),
        }
        sev = alert.get("severity", "info").lower()
        plain_badge, html_badge = badges.get(sev, badges["info"])
        
        source = alert.get("source", "System")
        time_str = self._utc_to_local(alert["timestamp"])  # ✅ Конвертируем здесь
        
        html = f"{html_badge} <b>{source}</b><br>"
        plain = f"{plain_badge} {source}\n"

        if alert.get("location"):
            html += f"📍 <b>Место:</b> {alert['location']}<br>"
            plain += f"📍 Место: {alert['location']}\n"
        if alert.get("description"):
            html += f"📝 <b>Описание:</b> {alert['description']}<br>"
            plain += f"📝 Описание: {alert['description']}\n"
        if alert.get("parameter"):
            val = alert.get("value", "N/A")
            unit = alert.get("unit", "")
            html += f"📊 <b>{alert['parameter']}:</b> <code>{val} {unit}</code><br>"
            plain += f"📊 {alert['parameter']}: {val} {unit}\n"
            if alert.get("min_val") is not None:
                html += f"&nbsp;&nbsp;&nbsp;📉 Норма: <code>{alert['min_val']} – {alert['max_val']}</code><br>"
                plain += f"    Норма: {alert['min_val']} – {alert['max_val']}\n"

        html += f"🕒 {time_str}"
        plain += f"🕒 {time_str}"
        
        return plain, html

    # def format_message(self, alert: dict) -> str:
    #     icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
    #     icon = icons.get(alert.get("severity", "info"), "📢")
        
    #     msg = f"{icon} **{alert.get('source', 'Unknown')}**\n"
    #     if alert.get("location"):
    #         msg += f" **Место:** {alert['location']}\n"
    #     if alert.get("description"):
    #         msg += f"📝 **Описание:** {alert['description']}\n"
    #     if alert.get("parameter"):
    #         val = alert.get("value", "N/A")
    #         msg += f"📊 **{alert['parameter']}:** `{val} {alert.get('unit', '')}`\n"
    #     msg += f"🕒 {alert['timestamp'][:19].replace('T', ' ')}"
    #     return msg

    # async def poll_and_route(self):
    #     try:
    #         # Запрашиваем ВСЕ новые события
    #         params = {"limit": 50}
    #         if self.last_check:
    #             params["since"] = self.last_check
                
    #         resp = requests.get(API_URL_ALL, params=params, timeout=5)
    #         if resp.status_code != 200: return
            
    #         events = resp.json()
    #         if not events: return

    #         # Сортируем по времени
    #         events.sort(key=lambda x: x["timestamp"])
            
    #         logger.info(f"📥 Received {len(events)} new events from API")

    #         for evt in events:
    #             source = evt.get("source", "")
    #             # Ищем комнату: сначала точное совпадение, потом дефолт
    #             target_room = self.mappings.get(source) or self.default_room
                
    #             if not target_room:
    #                 logger.warning(f"⚠️ No route defined for source: '{source}'")
    #                 continue

    #             await self.send(target_room, self.format_message(evt))
    #             await asyncio.sleep(1.5) # Защита от спама
            
    #         # Обновляем маркер последнего чтения
    #         if events:
    #             self.last_check = events[-1]["timestamp"]
    #             logger.info(f"🚀 Routed {len(events)} messages successfully")

    #     except Exception as e:
    #         logger.error(f"Poll error: {e}")

    async def poll_and_route(self):
        try:
            # Запускаем синхронный requests в отдельном потоке, чтобы не блокировать Matrix-цикл
            logger.debug(f"📡 Polling API with since={self.last_check}")
            resp = await asyncio.to_thread(
                requests.get, 
                API_URL_ALL, 
                params={"limit": 50, "since": self.last_check},
                timeout=10  # Увеличили с 5 до 10 сек
            )
            if resp.status_code != 200: 
                logger.warning(f"API returned {resp.status_code}")
                return
            
            events = resp.json()
            if not events: 
                return

            events.sort(key=lambda x: x["timestamp"])
            logger.info(f"📥 Received {len(events)} new events")

            for evt in events:
                source = evt.get("source", "")
                target_room = self.mappings.get(source) or self.default_room
                
                if not target_room:
                    logger.warning(f"⚠️ No route for source: '{source}'")
                    continue
                plain, html = self.format_message(evt)
                await self.send(target_room, plain, html)    
                await asyncio.sleep(1.0)  # Чуть быстрее, но безопасно
            
            if events:
                # ✅ Сохраняем позицию ПОСЛЕ успешной рассылки
                last_ts=events[-1]["timestamp"]
                self.last_check = last_ts
                save_cursor(last_ts)
                logger.info(f"🚀 Routed {len(events)} messages")
            
            
            
            

        except requests.exceptions.Timeout:
            logger.warning("⏱️ API read timeout (server busy?)")
        except Exception as e:
            logger.error(f"Poll error: {e}")

    async def run(self):
        if not await self.login(): return
        logger.info(" Universal Alert Bot running...")
        token = load_token()
        
        while True:
            try:
                # Sync нужен чтобы бот был "живым" в Matrix
                resp = await self.client.sync(timeout=30000, since=token)
                if isinstance(resp, SyncResponse):
                    token = resp.next_batch
                    save_token(token)
                    
                    # Проверяем, не изменился ли конфиг routes.json (на случай hot-reload)
                    # Для простоты перечитываем раз в цикл (дешево)
                    self.load_routes() 
                    
                    await self.poll_and_route()
            except Exception as e:
                logger.error(f"Sync error: {e}")
            await asyncio.sleep(config.POLL_INTERVAL)

    async def close(self):
        if self.client.access_token: await self.client.close()

async def main():
    bot = AlertBot()
    try: await bot.run()
    except KeyboardInterrupt: await bot.close()

if __name__ == "__main__":
    asyncio.run(main())