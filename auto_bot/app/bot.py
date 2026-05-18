import asyncio
import re
import os
import time
import logging
from datetime import datetime, timezone, timedelta
from nio import AsyncClient, SyncResponse, RoomMessageText, LoginResponse, InviteEvent, ErrorResponse
import config
from service.glonass_api import GlonassClient
from token_manager import load_token, save_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
os.makedirs("./matrix_store", exist_ok=True)

# ==============================================================================
# 📦 РЕЕСТР КОМАНД С АЛИАСАМИ (модульный уровень)
# ==============================================================================
COMMANDS = {
    "show_buses":  {"handler": "_show_vehicles", "icon": "🚌", "aliases": ["bus","автобус","автобусы"], "ids_key": "BUS_IDS"},
    "show_trucks": {"handler": "_show_vehicles", "icon": "🚛", "aliases": ["truck","грузовик","грузовики","груз"], "ids_key": "TRUCK_IDS"},
    "show_cars":   {"handler": "_show_vehicles", "icon": "🚗", "aliases": ["car","легковая","легковые","машина"], "ids_key": "CAR_IDS"},
    "show_all":    {"handler": "_show_vehicles", "icon": "📋", "aliases": ["all","все","всё"], "ids_key": "ALL"},
    "show_menu":   {"handler": "_show_menu", "icon": "🔄", "aliases": ["menu","меню","старт","помощь","help"]},
    "show_vehicle":{"handler": "_show_vehicle_detail", "icon": "📍", "aliases": ["info","инфо","детали","где","loc"]},
    "add_widget": {
        "handler": "cmd_widget",  # <-- точное имя метода в классе
        "icon": "🧩",
        "aliases": ["виджет", "widget", "карта", "map"],  # <-- "виджет" здесь?
        "description": "Добавить виджет: !виджет <type> [args]"
    },
    "select_vehicle":{"handler": "_handle_selection", "icon": "🔢", "aliases": ["выбор","select","pick","номер"]}
}

# Обратный индекс для быстрого поиска: alias -> command_key
ALIAS_MAP = {}
for cmd_key, cfg in COMMANDS.items():
    for alias in cfg["aliases"]:
        ALIAS_MAP[alias.lower()] = cmd_key

# ==============================================================================
# 🤖 КЛАСС БОТА
# ==============================================================================
class AutoBot:
    def __init__(self):
        self.client = AsyncClient(
            config.HOMESERVER, 
            config.USERNAME,
            store_path="./matrix_store"
        )
        self.glonass = GlonassClient()
        self.tz = self._parse_tz()
        # Кэш для !выбор <номер>: {(room_id, user_id): {"ids": [], "ts": float}}
        self.selection_cache = {}
        
        self.client.add_event_callback(self._auto_join_room, InviteEvent)

    async def _auto_join_room(self, room, event):
        """Колбэк: автоматически заходит в комнату при получении приглашения"""
        
        # ✅ 1. Проверяем тип события — игнорируем служебные (InviteNameEvent и др.)
        if not isinstance(event, InviteEvent):
            return
        
        try:
            # ✅ 2. ВАЖНО: room_id — это ПЕРВЫЙ аргумент колбэка, не event.room_id!
            room_id = room if isinstance(room, str) else getattr(room, 'room_id', None)
            if not room_id:
                logger.warning("⚠️ Не удалось получить room_id из колбэка")
                return
            
            # ✅ 3. Заходим в комнату
            await self.client.join(room_id)
            logger.info(f"✅ Бот автоматически вошёл в комнату {room_id}")
            
            # ✅ 4. Отправляем приветствие (self.send ожидает строку room_id)
            await self.send(room_id, "👋 Спасибо за приглашение! Напишите `!меню` или `!виджет bus <ID>`, чтобы начать.")
            
        except Exception as e:
            logger.error(f"❌ Ошибка входа в комнату: {e}", exc_info=True)

    def _parse_tz(self):
        m = re.match(r'^(?:UTC)?([+-]?)(\d{1,2})(?::?(\d{2}))?$', config.TIMEZONE, re.I)
        if m:
            sign = -1 if m.group(1) == '-' else 1
            return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
        return timezone.utc

    # ---------------------- AUTH & SEND ----------------------
    # async def login(self):
    #     try:
            
    #         # 1. Пробуем использовать токен из конфига
    #         token = config.ACCESS_TOKEN or os.getenv("ACCESS_TOKEN")
            
    #         if token:
    #             logger.info("🔑 Using ACCESS_TOKEN for login...")
    #             self.client.access_token = token
    #             # login() без пароля использует установленный токен
    #             resp = await self.client.login() 
    #         else:
    #             # 2. Если токена нет, входим по паролю
    #             logger.info("🔑 No token found, logging in with PASSWORD...")
    #             resp = await self.client.login(config.PASSWORD)
    #         # resp = await self.client.login(config.PASSWORD)
            
    #         if isinstance(resp, LoginResponse):
    #             logger.info("✅ AutoBot logged in")
    #             logger.info(f"   Device ID: {resp.device_id}")
    #             logger.info(f"   User ID: {resp.user_id}")
    #             logger.info(f"   ACCESS TOKEN: {resp.access_token}")
    #             # 🔑 Загружаем ключи E2EE (после логина!)
    #             await self.client.load_store()
    #             logger.info("✅ E2EE keys loaded from ./matrix_store")
    #             return True
    #         else:
    #             logger.error(f"❌ Login failed: {resp}")
    #         return False
    #     except Exception as e:
    #         logger.error(f"❌ Login failed: {e}")
    #         return False

# В main.py, внутри класса AutoBot

    async def login(self):
        # 1. Берем токен из конфига или переменных
        token = config.ACCESS_TOKEN or os.getenv("ACCESS_TOKEN")
        
        try:
            if token:
                logger.info("🔑 Using ACCESS_TOKEN...")
                self.client.access_token = token
                # ✅ ВАЖНО: вызывать БЕЗ аргументов! matrix-nio сам подхватит self.client.access_token
                resp = await self.client.login(token=token) 
            else:
                logger.info("🔑 No token found, logging in with PASSWORD...")
                if not config.PASSWORD:
                    logger.error("❌ Ни ACCESS_TOKEN, ни PASSWORD не заданы!")
                    return False
                resp = await self.client.login(password=config.PASSWORD)

            # 2. Проверяем результат
            if isinstance(resp, LoginResponse):
                logger.info(f"✅ Logged in as {resp.user_id} (device: {resp.device_id})")
                
                # 🔑 ЗАГРУЗКА E2EE КЛЮЧЕЙ (строго после успешного login!)
                await self.client.load_store()
                logger.info("✅ E2EE keys loaded from ./matrix_store")
                
                return True
            elif isinstance(resp, ErrorResponse):
                logger.error(f"❌ Login failed: {resp.message} ({resp.status_code})")
                return False
            else:
                logger.error(f"❌ Unexpected login response: {resp}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Login exception: {e}", exc_info=True)
            return False


    async def send(self, room: str, text: str, markdown: bool = True):
        if markdown:
            html_text = text
            html_text = re.sub(r'\[([^\]]+)\]\((matrix:action:[^\)]+)\)', r'<a href="\2">\1</a>', html_text)
            html_text = re.sub(r'\*\*([^\*]+)\*\*', r'<strong>\1</strong>', html_text)
            html_text = re.sub(r'_([^_]+)_', r'<em>\1</em>', html_text)
            html_text = html_text.replace('\n', '<br>')
            await self.client.room_send(room, "m.room.message", {
                "msgtype": "m.text", "body": text,
                "format": "org.matrix.custom.html", "formatted_body": html_text
            })
        else:
            await self.client.room_send(room, "m.room.message", {"msgtype": "m.text", "body": text})

    # ---------------------- PARSING & DISPATCH ----------------------
    def parse_command(self, text: str) -> tuple[str | None, str | None]:
        """Возвращает (internal_command_name, args) или (None, None)"""
        body = text.strip()
        if not body.startswith('!'):
            return None, None
        parts = body[1:].split(maxsplit=1)
        cmd_word = parts[0].lower()
        args = parts[1] if len(parts) > 1 else None
        return ALIAS_MAP.get(cmd_word), args

    async def message_handler(self, room: str, event: RoomMessageText):
        if event.sender == self.client.user_id:
            return

        cmd_name, args = self.parse_command(event.body)
        if cmd_name:
            logger.info(f"⚡ {event.sender} → {cmd_name} (args: {args})")
            await self._dispatch_command(room, event.sender, cmd_name, args)
        else:
            await self.send(room, "💡 Доступные команды:\n`!меню` `!автобусы` `!инфо <ID>` `!выбор <номер>`\n\n" + self.get_main_menu(self.get_role(event.sender)))

    async def _dispatch_command(self, room: str, sender: str, cmd_name: str, args: str | None):
        try:
            cfg = COMMANDS[cmd_name]
            handler = getattr(self, cfg["handler"], None)
            if not handler:
                logger.error(f"❌ Handler {cfg['handler']} не найден для {cmd_name}")
                return

            role = self.get_role(sender)
            if cmd_name in ("show_trucks", "show_cars", "show_all") and role != "all":
                await self.send(room, "⛔ Доступ к этой категории ограничен.")
                return

            # ✅ ИСПРАВЛЕНО: маршрутизация по наличию ключа ids_key в конфиге команды
            if cfg.get("ids_key"):
                ids_key = cfg["ids_key"]
                if ids_key == "ALL":
                    ids = config.BUS_IDS + config.TRUCK_IDS + config.CAR_IDS
                else:
                    ids = getattr(config, ids_key, [])
                logger.debug(f"📦 Маршрутизация: {cmd_name} → handler={cfg['handler']}, ids_count={len(ids)}")
                await handler(room, sender, ids, cfg["icon"])

            elif cmd_name == "show_vehicle":
                vid = self._extract_vehicle_id(args)
                if vid:
                    await handler(room, sender, vid)
                else:
                    await self.send(room, "❓ Формат: `!инфо <ID>` (пример: `!инфо 413422`)")

            elif cmd_name == "select_vehicle":
                await handler(room, sender, args)

            elif cmd_name == "show_menu":
                await handler(room, sender)
            
            elif cmd_name == "add_widget":
                await handler(room, sender, args)
                
        except Exception as e:
            logger.error(f"💥 Ошибка в диспетчере команд `{cmd_name}`: {e}", exc_info=True)
            await self.send(room, f"⚠️ Произошла ошибка при обработке команды. Подробности в логах.")

    async def _show_vehicles(self, room: str, sender: str, ids: list[int], icon: str):
        logger.info(f"📡 Запрос GLONASS для {icon}, ID: {ids}")
        try:
            vehicles = await self.glonass.get_last_data(ids)
        except Exception as e:
            logger.error(f"❌ Ошибка запроса к GLONASS API: {e}", exc_info=True)
            await self.send(room, f"⚠️ Не удалось получить данные от системы мониторинга.")
            return

        if not vehicles:
            await self.send(room, f"⚠️ Нет активных ТС в категории {icon} или они оффлайн.\n`!меню` — главное меню")
            return

        # Кэшируем список на 5 минут для !выбор
        self.selection_cache[(room, sender)] = {"ids": [v.vehicleId for v in vehicles], "ts": time.time()}

        lines = [f"{icon} **Активные ТС ({len(vehicles)}):**\n"]
        for idx, v in enumerate(vehicles, 1):
            vnum = v.vehicleNumber or f"ID:{v.vehicleId}"
            lines.append(f"{idx}. {vnum} (ID:{v.vehicleId})")
        
        lines.append(f"\n💡 Для деталей: `!инфо <ID>` или `!выбор <номер>`")
        lines.append("`!меню` — главное меню")
        await self.send(room, "\n".join(lines))

    def _extract_vehicle_id(self, args: str | None) -> int | None:
        if args and args.isdigit():
            return int(args)
        return None

    # ---------------------- COMMAND HANDLERS ----------------------
    async def _show_vehicles(self, room: str, sender: str, ids: list[int], icon: str):
        vehicles = await self.glonass.get_last_data(ids)
        if not vehicles:
            await self.send(room, f"⚠️ Нет активных ТС в категории {icon}.\n`!меню` — главное меню")
            return

        # Кэшируем список на 5 минут для !выбор
        self.selection_cache[(room, sender)] = {"ids": [v.vehicleId for v in vehicles], "ts": time.time()}

        lines = [f"{icon} **Активные ТС ({len(vehicles)}):**\n"]
        for idx, v in enumerate(vehicles, 1):
            vnum = v.vehicleNumber or f"ID:{v.vehicleId}"
            lines.append(f"{idx}. {vnum} (ID:{v.vehicleId})")
        
        lines.append(f"\n💡 Для деталей: `!инфо <ID>` или `!выбор <номер>`")
        lines.append("`!меню` — главное меню")
        await self.send(room, "\n".join(lines))

    async def _handle_selection(self, room: str, sender: str, args: str | None):
        if not args or not args.isdigit():
            await self.send(room, "❓ Формат: `!выбор <номер>` (например: `!выбор 1`)")
            return

        idx = int(args) - 1
        cache_key = (room, sender)
        cache = self.selection_cache.get(cache_key)

        if not cache or (time.time() - cache["ts"] > 300):
            await self.send(room, "⏳ Список устарел. Запросите `!автобусы` заново.")
            self.selection_cache.pop(cache_key, None)
            return

        if idx < 0 or idx >= len(cache["ids"]):
            await self.send(room, f"❌ Номер вне диапазона (допустимо 1-{len(cache['ids'])}).")
            return

        await self._show_vehicle_detail(room, sender, cache["ids"][idx])

    async def _show_vehicle_detail(self, room: str, sender: str, vid: int):
        role = self.get_role(sender)
        if role == "bus_only" and vid not in config.BUS_IDS:
            await self.send(room, "⛔ Доступ к этому ТС запрещен.\n`!меню` — главное меню")
            return

        data = await self.glonass.get_last_data([vid])
        if not data:
            await self.send(room, "ℹ️ Данные не найдены или ТС оффлайн.\n`!меню` — главное меню")
            return

        await self.send(room, self.format_vehicle_detail(data[0]))

    async def _show_menu(self, room: str, sender: str):
        await self.send(room, self.get_main_menu(self.get_role(sender)))

    def get_role(self, sender: str) -> str:
        return "all" if sender in config.VISIBLE_ALL_ROLES else "bus_only"

    def get_main_menu(self, role: str) -> str:
        menu = "🐝 **Мониторинг транспорта**\nВыберите категорию:\n"
        menu += "• `!автобусы` — 🚌 Список автобусов\n"
        if role == "all":
            menu += "• `!грузовики` — 🚛 Список грузовиков\n"
            menu += "• `!легковые` — 🚗 Список легковых\n"
            menu += "• `!все` — 📋 Весь транспорт\n"
        menu += "• `!меню` — ♻️ Обновить меню"
        return menu

    def format_vehicle_detail(self, v: object, prefix: str = "🚌") -> str:
        vnum = v.vehicleNumber or f"ID:{v.vehicleId}"
        coords = f"{v.latitude}, {v.longitude}"
        map_link = f"https://yandex.ru/maps/?pt={v.longitude},{v.latitude}&z=16&l=map"
        time_str = datetime.now(self.tz).strftime("%H:%M")
        
        return (
            f"📍 {prefix} **{vnum}**\n"
            f"📍 {v.address or coords}\n"
            f"🕒 Обновлено: {time_str}\n"
            f"[🗺️ Яндекс.Карты]({map_link})\n\n"
            "↩️ `!меню` — главное меню"
        )

    # ---------------------- SYNC LOOP ----------------------
    async def run(self):
        if not await self.login():
            return


        bot_start_time = int(time.time() * 1000)
        
        token = load_token()
        logger.info(f"👂 Bot starting... (token present: {bool(token)})")

        resp = await self.client.sync(timeout=30000, since=token, full_state=True)
        if not isinstance(resp, SyncResponse):
            logger.error("❌ Initial sync failed"); return

        token = resp.next_batch
        save_token(token)

        now_ms = int(time.time() * 1000)
        for rid, rdata in resp.rooms.join.items():
            for ev in rdata.timeline.events:
                if isinstance(ev, RoomMessageText):
                    if hasattr(ev, 'server_timestamp') and (now_ms - ev.server_timestamp > 60000):
                        if ev.server_timestamp < bot_start_time:
                            logger.debug(f"⏩ Пропускаем старое сообщение")
                            continue
                    await self.message_handler(rid, ev)

        logger.info("✅ State synced. Entering incremental loop...")
        while True:
            try:
                resp = await self.client.sync(timeout=30000, since=token)
                if not isinstance(resp, SyncResponse):
                    await asyncio.sleep(5); continue
                token = resp.next_batch
                save_token(token)

                now_ms = int(time.time() * 1000)
                for rid, rdata in resp.rooms.join.items():
                    for ev in rdata.timeline.events:
                        if isinstance(ev, RoomMessageText):
                            if hasattr(ev, 'server_timestamp') and (now_ms - ev.server_timestamp > 60000):
                                continue
                            await self.message_handler(rid, ev)
            except Exception as e:
                logger.error(f"Sync error: {e}"); await asyncio.sleep(5)
                
    async def cmd_widget(self, room: str, sender: str, args: str | None):
        """Универсальный установщик виджетов: !виджет <type> [args]"""
        if not config.WIDGET_ACCESS_TOKEN:
            await self.send(room, "⚠️ Ошибка: WIDGET_ACCESS_TOKEN не настроен в .env")
            return

        if not args:
            await self.send(room, self._get_widget_help())
            return

        parts = args.strip().split(maxsplit=1)
        widget_type = parts[0].lower()
        extra_args = parts[1] if len(parts) > 1 else ""

        cfg = config.WIDGET_TYPES.get(widget_type)
        if not cfg:
            await self.send(room, f"❌ Неизвестный тип виджета `{widget_type}`.\n" + self._get_widget_help())
            return

        if cfg.get("admin_only") and sender not in config.ADMINS:
            await self.send(room, "⛔ Этот виджет могут добавлять только администраторы.")
            return

        try:
            widget_url = self._build_widget_url(cfg, extra_args)
        except ValueError as e:
            await self.send(room, f"❌ Ошибка параметров: {e}")
            return

        content = {
            "type": "m.widget",
            "url": widget_url,
            "name": cfg["name"],
            "creator": sender,
            "data": {"type": widget_type, "args": extra_args}
        }

        try:
            # Уникальный state_key для этой комнаты и конфигурации
            state_key = f"widget_{widget_type}_{extra_args.replace(' ', '_')}"
            await self.client.room_send(
                room_id=room,
                message_type="im.vector.modular.widgets",
                content=content,
                state_key=state_key
            )
            await self.send(room, f"✅ Виджет «{cfg['name']}» установлен! 🎉")
        except Exception as e:
            logger.error(f"Widget install error: {e}")
            await self.send(room, f"⚠️ Ошибка при установке: {e}")

    def _build_widget_url(self, cfg: dict, args: str) -> str:
        """Собирает URL виджета, подставляя аргументы и токен"""
        base = config.SITE_BASE_URL
        pattern = cfg["url_pattern"]
        required = cfg.get("required_args", [])

        if required:
            arg_list = args.split()
            if len(arg_list) < len(required):
                missing = ", ".join(required[len(arg_list):])
                raise ValueError(f"Нужно указать: {missing}")
            for key, val in zip(required, arg_list):
                pattern = pattern.replace(f"{{{key}}}", val)

        # $matrix_room_id — магическая переменная, которую Element подставит сам
        return (
            f"{base}{pattern}"
            f"?token={config.WIDGET_ACCESS_TOKEN}"
            f"&room_id=$matrix_room_id"
        )

    def _get_widget_help(self) -> str:
        """Генерирует справку по доступным виджетам"""
        lines = ["💡 Доступные виджеты:"]
        for t, cfg in config.WIDGET_TYPES.items():
            args = " ".join(f"<{a}>" for a in cfg.get("required_args", []))
            lines.append(f"• `!виджет {t} {args}` — {cfg['description']}")
        return "\n".join(lines)

    async def close(self):
        await self.client.close()

# ==============================================================================
# 🚀 ENTRY POINT
# ==============================================================================
async def main():
    bot = AutoBot()
    try: 
        await bot.run()
    except KeyboardInterrupt: 
        logger.info("🛑 Stop")
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())