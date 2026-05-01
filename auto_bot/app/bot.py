import asyncio
import re
import logging
from datetime import datetime, timezone, timedelta
from nio import AsyncClient, SyncResponse, RoomMessageText, LoginResponse, ReactionEvent
import config
from service.glonass_api import GlonassClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class AutoBot:
    def __init__(self):
        self.client = AsyncClient(config.HOMESERVER, config.USERNAME)
        self.glonass = GlonassClient()
        self.tz = self._parse_tz()

    def _parse_tz(self):
        m = re.match(r'^(?:UTC)?([+-]?)(\d{1,2})(?::?(\d{2}))?$', config.TIMEZONE, re.I)
        if m:
            sign = -1 if m.group(1) == '-' else 1
            return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
        return timezone.utc

    async def login(self):
        resp = await self.client.login(config.PASSWORD)
        if isinstance(resp, LoginResponse):
            logger.info("✅ AutoBot logged in")
            return True
        logger.error(f"❌ Login failed: {resp}")
        return False

    # async def send(self, room: str, text: str):
        # await self.client.room_send(room, "m.room.message", {"msgtype": "m.text", "body": text})
    async def send(self, room: str, text: str, markdown: bool = True):
        """Отправка сообщения с опциональным markdown форматированием"""
        if markdown:
            # Конвертируем markdown в HTML для Element
            import re
            
            # Простая конвертация markdown ссылок [text](url) в HTML
            html_text = text
            # Конвертируем [text](url) в <a href="url">text</a>
            html_text = re.sub(
                r'\[([^\]]+)\]\((matrix:action:[^\)]+)\)',
                r'<a href="\2">\1</a>',
                html_text
            )
            # Жирный текст **text**
            html_text = re.sub(r'\*\*([^\*]+)\*\*', r'<strong>\1</strong>', html_text)
            # Курсив _text_
            html_text = re.sub(r'_([^_]+)_', r'<em>\1</em>', html_text)
            # Переносы строк
            html_text = html_text.replace('\n', '<br>')
            
            await self.client.room_send(
                room,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": text,  # Plain text fallback
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_text  # HTML версия
                }
            )
        else:
            await self.client.room_send(
                room,
                "m.room.message",
                {"msgtype": "m.text", "body": text}
            )

    def get_role(self, sender: str) -> str:
        return "all" if sender in config.VISIBLE_ALL_ROLES else "bus_only"

    def get_main_menu(self, role: str) -> str:
        menu = "🐝 **Мониторинг транспорта**\nВыберите категорию:\n"
        menu += "[🚌 Автобусы](matrix:action:cmd_bus)\n"
        if role == "all":
            menu += "[🚛 Грузовики](matrix:action:cmd_truck)\n"
            menu += "[🚗 Легковые](matrix:action:cmd_car)\n"
            menu += "[📋 Все ТС](matrix:action:cmd_all)\n"
        menu += "[ Обновить список](matrix:action:cmd_menu)"
        return menu

    def format_vehicle_list(self, vehicles: list, category: str) -> str:
        if not vehicles:
            return f"⚠️ Нет данных или транспорт оффлайн.\n\n[↩️ Главное меню](matrix:action:cmd_menu)"
        
        emoji = {"bus": "🚌", "truck": "", "car": "🚗", "all": "📋"}.get(category, "🚍")
        lines = [f"{emoji} **Активные ТС ({len(vehicles)}):**"]
        
        for v in vehicles:
            # Каждый номер -> кликабельная кнопка для деталей
            link = f"matrix:action:loc_{v.vehicleId}"
            lines.append(f"[{v.vehicleNumber}]({link})")
            
        lines.append("\n_Нажмите на номер для деталей и карты_")
        lines.append("[️ Главное меню](matrix:action:cmd_menu)")
        return "\n".join(lines)

    def format_vehicle_detail(self, v: object, prefix: str = "🚌") -> str:
        
        vnum = v.vehicleNumber or f"ID:{v.vehicleId}"
        coords = f"{v.latitude}, {v.longitude}"
        map_link = f"https://yandex.ru/maps/?pt={v.longitude},{v.latitude}&z=16&l=map"
        time_str = datetime.now(self.tz).strftime("%H:%M")
        
        msg = (
            f"📍 {prefix} **{v.vehicleNumber}**\n"
            f"📍 {v.address or coords}\n"
            f"🕒 Обновлено: {time_str}\n"
            f"[ Открыть на Яндекс.Картах]({map_link})\n\n"
            "[️ Главное меню](matrix:action:cmd_menu)"
        )
        return msg

    async def handle_action(self, room: str, sender: str, action: str):
        role = self.get_role(sender)
        try:
            # 1. Детали конкретного ТС
            if action.startswith("loc_"):
                vid = int(action.split("_")[1])
                if role == "bus_only" and vid not in config.BUS_IDS:
                    await self.send(room, "⛔ Доступ к этому ТС запрещен.\n[↩️ Главное меню](matrix:action:cmd_menu)")
                    return
                
                data = await self.glonass.get_last_data([vid])
                if not data:
                    await self.send(room, "ℹ️ Данные не найдены или ТС оффлайн.\n[↩️ Главное меню](matrix:action:cmd_menu)")
                    return
                await self.send(room, self.format_vehicle_detail(data[0]))
                return

            # 2. Навигация по меню
            if action in ("cmd_menu", "cmd_help"):
                await self.send(room, self.get_main_menu(role))
            elif action == "cmd_bus":
                vehicles = await self.glonass.get_last_data(config.BUS_IDS)
                await self.send(room, self.format_vehicle_list(vehicles, "bus"))
            elif action == "cmd_truck" and role == "all":
                vehicles = await self.glonass.get_last_data(config.TRUCK_IDS)
                await self.send(room, self.format_vehicle_list(vehicles, "truck"))
            elif action == "cmd_car" and role == "all":
                vehicles = await self.glonass.get_last_data(config.CAR_IDS)
                await self.send(room, self.format_vehicle_list(vehicles, "car"))
            elif action == "cmd_all" and role == "all":
                all_ids = config.BUS_IDS + config.TRUCK_IDS + config.CAR_IDS
                vehicles = await self.glonass.get_last_data(all_ids)
                await self.send(room, self.format_vehicle_list(vehicles, "all"))
            else:
                await self.send(room, "❓ Неизвестное действие\n[↩️ Главное меню](matrix:action:cmd_menu)")
                
        except Exception as e:
            logger.error(f"Action error: {e}", exc_info=True)
            await self.send(room, f" Ошибка обработки: {str(e)}\n[↩️ Главное меню](matrix:action:cmd_menu)")

    async def message_handler(self, room: str, event: RoomMessageText):
        sender = event.sender
        if sender == self.client.user_id:
            return

        body = event.body.strip()

        # 🔹 Перехватываем клики по кнопкам Element
        if body.startswith("matrix:action:"):
            action = body.split(":", 2)[2]
            await self.handle_action(room, sender, action)
            return

        # 🔹 Если пользователь пишет текст -> показываем меню
        await self.send(room, "Используйте кнопки ниже для навигации:\n" + self.get_main_menu(self.get_role(sender)))

    async def run(self):
        if not await self.login(): 
            return
        
        logger.info("👂 AutoBot listening (Element Quick Replies mode)...")
        token = None
        
        while True:
            try:
                resp = await self.client.sync(timeout=30000, since=token)
                if not isinstance(resp, SyncResponse):
                    await asyncio.sleep(5)
                    continue
                token = resp.next_batch

                # Авто-вступление в ЛС
                for rid in resp.rooms.invite.keys():
                    await self.client.join(rid)
                    await self.send(rid, " Привет! Я бот мониторинга транспорта.")
                    # Отправляем меню сразу (проверка ролей произойдёт при клике)
                    await self.send(rid, self.get_main_menu("all"))
                    
                # Чтение сообщений
                for rid, rdata in resp.rooms.join.items():
                    for ev in rdata.timeline.events:
                        if isinstance(ev, RoomMessageText):
                            await self.message_handler(rid, ev)
                            
            except Exception as e:
                logger.error(f"Sync error: {e}")
                await asyncio.sleep(5)

    async def close(self):
        await self.client.close()

async def main():
    bot = AutoBot()
    try: 
        await bot.run()
    except KeyboardInterrupt: 
        logger.info("🛑 Stop")
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())