import asyncio
import logging
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from nio import AsyncClient, SyncResponse, LoginResponse
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
        await self.client.room_send(room, "m.room.message", {
            "msgtype": "m.text", "body": plain,
            "format": "org.matrix.custom.html", "formatted_body": html
        })

    def _utc_to_local(self, utc_iso: str) -> str:
        """Конвертирует ISO-строку из UTC в настроенный часовой пояс"""
        try:
            dt = datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return utc_iso[:19].replace("T", " ")

    def format_message(self, alert: dict) -> tuple[str, str]:
        icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        sev = alert.get("severity", "info")
        icon = icons.get(sev, "📢")
        source = alert.get("source", "Unknown")
        meta = self.sources.get(source, {})
        
        
        display_name = meta.get("comment") or source
        severity = alert.get("severity", meta.get("severity", "info"))
        icon = icons.get(severity, "📢")

        # ✅ Используем корректную конвертацию вместо среза
        time_str = self._utc_to_local(alert["timestamp"])
        
        html = f" <b>📣 Источник: </b> <u>{display_name}</u><br>"
        plain = f"[{sev.upper()}] {display_name}\n"

        if alert.get("location"):
            html += f"📍 <b>Место:</b> {alert['location']}<br>"
            plain += f"📍 Место: {alert['location']}\n"
        if alert.get("description"):
            html += f"📝 <b>Описание:</b> {alert['description']}<br>"
            plain += f"📝 Описание: {alert['description']}\n"
        if alert.get("parameter"):
            val, unit = alert.get("value", "N/A"), alert.get("unit", "")
            html += f"📊 <b>{alert['parameter']}:</b> <code>{val} {unit}</code><br>"
            plain += f"📊 {alert['parameter']}: {val} {unit}\n"
            if alert.get("min_val") is not None:
                html += f"&nbsp;&nbsp;&nbsp;📉 Норма: <code>{alert['min_val']} – {alert['max_val']}</code><br>"
                plain += f"    Норма: {alert['min_val']} – {alert['max_val']}\n"

        html += f"🕒 {time_str}"
        plain += f"🕒 {time_str}"
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
                plain, html = self.format_message(evt)
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