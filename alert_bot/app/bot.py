import asyncio
import logging
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from nio import AsyncClient, SyncResponse, LoginResponse, ErrorResponse
import config
from cursor_manager import load_cursor, save_cursor
from token_manager import load_token, save_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "http://alert_api:8000/api/v1"

class AlertBot:
    def __init__(self):
        self.client = AsyncClient(config.HOMESERVER, config.USERNAME)
        self.routes = {}  # Будет загружено с API
        self.sources = {}  # Будет загружено с API
        self.last_check = load_cursor() or datetime.now(timezone.utc).isoformat()
        if not load_cursor(): save_cursor(self.last_check)

    async def login(self):
        resp = await self.client.login(config.PASSWORD)
        return isinstance(resp, LoginResponse)

    async def fetch_routes(self):
        try:
            resp = requests.get(
                f"{API_BASE}/config/routes",
                headers={"X-Bot-Token": config.BOT_TOKEN},
                timeout=5
            )
            if resp.status_code == 200:
                self.sources = resp.json()
                logger.info(f"🗺️ Routes updated: {len(self.routes)} sources")
                # Логируем комментарии для наглядности
                for src, meta in self.sources.items():
                    logger.debug(f"   • {src} → {meta['comment']} ({meta['room']})")
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch routes: {e} (using cached)")


    
    async def send(self, room: str, plain: str, html: str):
        # ✅ content ОБЯЗАН быть словарём по спецификации Matrix
        content = {
            "msgtype": "m.text",
            "body": plain,
            "format": "org.matrix.custom.html",
            "formatted_body": html
        }
        
        response = await self.client.room_send(room, "m.room.message", content)
        
        if isinstance(response, ErrorResponse):
            if response.status_code == "M_LIMIT_EXCEEDED":
                retry_after = getattr(response, "retry_after_ms", 5000) / 1000
                print(f"⏳ Rate limited в {room}. Ждём {retry_after:.1f}с...")
                await asyncio.sleep(retry_after)
                await self.client.room_send(room, "m.room.message", content)
            else:
                print(f"❌ {room}: {response.status_code} - {response.message}")
                
        await asyncio.sleep(0.25)

    def _utc_to_local(self, utc_iso: str) -> str:
        """Конвертирует ISO-строку из UTC в настроенный часовой пояс"""
        try:
            dt = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return utc_iso[:19].replace("T", " ")

    
    
    def format_message(self, alert: dict) -> tuple[str, str]:
        try:
            source = alert.get("source", "unknown")
            meta = self.sources.get(source, {})
            display_name = meta.get("comment") or source
            severity = alert.get("severity", meta.get("severity", "info"))
            
            icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
            severity_descriptions = {"info": "ИНФОРМАЦИЯ", "warning": "ВНИМАНИЕ", "critical": "КРИТИЧНО"}
            icon = icons.get(severity, "📢")
            severity_description = severity_descriptions.get(severity, "ИНФОРМАЦИЯ")
            time_str = self._utc_to_local(alert["timestamp"])
            
            html = f"<hr>{icon} <b>{severity_description}</b><br>"
            plain = f"[{severity.upper()}] {severity_description}\n"

            # 📋 ДИНАМИЧЕСКИЙ КОНТЕКСТ (ключ-значение)
            if alert.get("context"):
                html += "<b>📋 Контекст:</b><br>"
                plain += "📋 Контекст:\n"
                for key, val in alert["context"].items():
                    html += f"&nbsp;&nbsp;&nbsp;• <b>{key}:</b> {val}<br>"
                    plain += f"    • {key}: {val}\n"
                html += "<br>"  # Отступ после контекста
                plain += "\n"

            if alert.get("location"):
                html += f"📍 <b>Место:</b> {alert['location']}<br>"
                plain += f"📍 Место: {alert['location']}\n"
            
            if alert.get("description"):
                html += f"📝 <b>Описание:</b> {alert['description']}<br>"
                plain += f"📝 Описание: {alert['description']}\n"
           
            if alert.get("product"):
                html += f"⚗ <b>Продукт:</b> {alert['product']}<br>"
                plain += f"⚗ Продукт: {alert['product']}\n"
           
            
            if alert.get("parameter"):
                val, unit = alert.get("value", "N/A"), alert.get("unit", "")
                param_name = alert['parameter']
                
                # 🎨 HTML версия с HR и UL (iOS-friendly)
                html += f"""
                <div style="background:#fff8dc; border:2px solid #f59e0b; border-radius:8px; margin:12px 0; overflow:hidden;">
                    <div style="text-align:center; background:#fbbf24; color:#78350f; padding:10px 8px; font-size:1.1em; font-weight:bold; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                        🎯 КОНТРОЛИРУЕМЫЙ ПАРАМЕТР
                    </div>
                    
                    <div style="padding:0 12px;">
                        <hr style="border:none; border-top:1px solid #fcd34d; margin:0;">
                    </div>
                    
                    <ul style="list-style:none; padding:0 12px; margin:0;">
                        <li style="padding:10px 0; border-bottom:1px solid #fcd34d;">
                            <b style="color:#92400e; display:block; margin-bottom:4px;">Параметр:</b> 
                            <code style="background:#fef3c7; padding:4px 8px; border-radius:4px; font-size:1.1em; color:#78350f; word-break:break-word; display:inline-block;">{param_name}</code>
                        </li>
                        <li style="padding:10px 0; border-bottom:1px solid #fcd34d;">
                            <b style="color:#92400e; display:block; margin-bottom:4px;">Значение:</b> 
                            <span style="font-size:1.4em; font-weight:bold; color:#dc2626;">{val}</span>
                            <span style="color:#78350f; font-size:0.9em; margin-left:4px;">{unit}</span>
                        </li>
                """
                
                if alert.get("min_val") is not None:
                    html += f"""
                        <li style="padding:10px 0;">
                            <b style="color:#92400e; display:block; margin-bottom:4px;">📊 Норма:</b> 
                            <span style="font-family:monospace; background:#fef3c7; padding:4px 8px; border-radius:4px; color:#166534; font-weight:bold; display:inline-block;">
                                {alert['min_val']} – {alert['max_val']} {unit}
                            </span>
                        </li>
                    """
                
                html += """
                    </ul>
                    <div style="padding:0 12px; margin-top:8px;">
                        <hr style="border:none; border-top:1px solid #fcd34d; margin:0;">
                    </div>
                </div>
                """
                
                # 📝 Plain text
                plain += f"🎯 КОНТРОЛИРУЕМЫЙ ПАРАМЕТР\n"
                plain += f"{'='*40}\n"
                plain += f"📍 {param_name}\n"
                plain += f"💠 Значение: {val} {unit}\n"
                if alert.get("min_val") is not None:
                    plain += f"📊 Норма: {alert['min_val']} – {alert['max_val']} {unit}\n"
                plain += f"{'='*40}\n\n"
                
                html += "<br>"  # Отступ после контекста
                plain += "\n"
            
            
            if alert.get("meta"):
                html += "<b>🧧 Метаданные:</b><br>"
                plain += "🧧 Метаданные:\n"
                for key, val in alert["meta"].items():
                    html += f"&nbsp;&nbsp;&nbsp;• <b>{key}:</b> {val}<br>"
                    plain += f"    • {key}: {val}\n"
                plain += f"{'='*40}\n\n"
                # html += "<br>"  # Отступ после контекста
                # plain += "\n"    
            
        except Exception as e:
            logger.error(f"Error formatting message: {e}")
            return None
        
        return plain, html

    async def poll_and_route(self):
        try:
            resp = await asyncio.to_thread(
                requests.get, f"{API_BASE}/stream/all",
                params={"limit": 50, "since": self.last_check}, 
                headers={"X-Bot-Token": config.BOT_TOKEN},
                timeout=10
            )
            if resp.status_code != 200: return
            events = [e for e in resp.json() if e["timestamp"] > self.last_check]
            events.sort(key=lambda x: x["timestamp"])
            
            if not events: return
            logger.info(f"📥 {len(events)} new events")
            
            for evt in events:
                source = evt.get("source")
                meta = self.sources.get(source)
                # room = self.routes.get(evt["source"]) or config.DEFAULT_ROOM
                if not meta or not meta.get("room"):
                    logger.warning(f"⚠️ No route for {evt['source']}")
                    continue
                
                room = meta["room"]
                
                # 🔒 Защита: проверяем, что format_message вернул кортеж
                result = self.format_message(evt)
                if not result or not isinstance(result, (tuple, list)) or len(result) != 2:
                    logger.warning(f"⚠️ format_message returned invalid result for {evt.get('id')}")
                    continue
                
                # plain, html = self.format_message(evt)
                plain, html = result
                await self.send(room, plain, html)
                await asyncio.sleep(1.0)
                
            save_cursor(events[-1]["timestamp"])
            self.last_check = events[-1]["timestamp"]
            logger.info(f"🚀 Routed {len(events)} messages")
        except Exception as e:
            logger.error(f"Poll error: {e}")

    async def run(self):
        if not await self.login(): return
        await self.fetch_routes()  # Загружаем маршруты сразу
        logger.info("👂 Alert Bot running...")
        token = load_token()
        
        while True:
            try:
                resp = await self.client.sync(timeout=30000, since=token)
                if isinstance(resp, SyncResponse):
                    token = resp.next_batch
                    save_token(token)
                    await self.fetch_routes()  # Обновляем маршруты раз в цикл
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