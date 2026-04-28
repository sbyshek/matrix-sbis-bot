import asyncio
import re
import logging
import os
from datetime import datetime, timezone
from collections import deque
from nio import AsyncClient, SyncResponse, RoomMessageText, LoginResponse

# Настройка логирования
from logger_config import setup_logging, get_logger
import config
setup_logging(config)
logger = get_logger(__name__)

from service.ldap import LdapSearcher
from service.router_os import RouterOsClient
from service.state_manager import ConnectionStateManager

# Конфигурация MikroTik
ROUTER_CONFIG = {
    'device_type': 'mikrotik_routeros',
    'host': config.ROUTEROS_HOST,
    'port': int(config.ROUTEROS_PORT or 22),
    'username': config.ROUTEROS_USERNAME,
    'password': config.ROUTEROS_PASSWORD,
}

class MatrixVPNBot:
    def __init__(self):
        self.client = AsyncClient(config.HOMESERVER, config.USERNAME)
        self.room_id = config.TARGET_ROOM
        self.state_manager = ConnectionStateManager()
        
        # Списки админов
        self.admin_usernames = config.ADMIN_USERNAMES  # {'a.avdeev', 'b.belkin'}
        self.admin_ids = config.ADMIN_LOGINS           # ['@a.avdeev:domain', '@b.belkin:domain']
        
        self.tz = config.TIMEZONE_TZINFO

    async def login(self):
        response = await self.client.login(config.PASSWORD)
        if isinstance(response, LoginResponse):
            logger.info(f"✅ Бот {config.USERNAME} залогинился")
            return True
        logger.error(f"❌ Ошибка логина: {response}")
        return False

    async def send_message(self, room_id: str, message: str):
        try:
            await self.client.room_send(
                room_id,
                "m.room.message",
                {"msgtype": "m.text", "body": message}
            )
        except Exception as e:
            logger.error(f"Failed to send message: {e}")

    async def notify_admins(self, message: str):
        """Отправка уведомления администраторам"""
        if not self.admin_ids:
            return
        mentions = " ".join(self.admin_ids)
        target = self.room_id 
        if target:
            await self.send_message(target, f"⚠️ {mentions}\n{message}")

    def extract_login(self, sender: str) -> str:
        try:
            return sender.split(':')[0].lstrip('@').lower()
        except Exception:
            return sender.lower()

    # ================= ПРОВЕРКА ПРАВ ДОСТУПА =================

    async def is_user_allowed(self, login: str) -> bool:
        """Проверяет, разрешено ли пользователю использовать бота (Группа AD или Админ)"""
        if login in self.admin_usernames:
            return True
        
        if not config.ALLOWED_USERS_GROUP:
            return True

        try:
            is_in_group, _ = await asyncio.to_thread(
                lambda: LdapSearcher().is_user_in_group(login, config.ALLOWED_USERS_GROUP)
            )
            return is_in_group
        except Exception as e:
            logger.error(f"LDAP check failed for {login}: {e}")
            return False

    # ================= КОМАНДЫ ПОЛЬЗОВАТЕЛЯ =================

    async def _cmd_connect(self, room_id: str, login: str, duration: int):
        """Логика подключения"""
        # Поиск компьютера в LDAP
        comp_result = await asyncio.to_thread(
            lambda: LdapSearcher().search_and_parse(
                search_filter=f'(&(objectClass=person)(sAMAccountName={login}))'
            )
        )
        computer_name = comp_result[0] if isinstance(comp_result, tuple) else comp_result

        if not computer_name or computer_name.lower() in ('none', 'n/a'):
            await self.send_message(room_id, "❌ Не удалось определить ваш компьютер в LDAP. Администраторы уведомлены.")
            await self.notify_admins(f"Пользователь {login} попытался подключиться, но msDS-PrimaryComputer пуст.")
            return

        client_ip = f"{computer_name}.st-oil.local"

        if self.state_manager.is_user_connected(login):
            await self.send_message(room_id, f"⛔ Доступ уже занят. Используйте !status")
            return

        if not self.state_manager.reserve(login, client_ip, duration):
            await self.send_message(room_id, "❌ Не удалось зарезервировать доступ")
            return

        try:
            await asyncio.to_thread(
                lambda: RouterOsClient(ROUTER_CONFIG).__enter__().add_to_address_list(
                    address=client_ip,
                    list_name=config.ROUTEROS_ADDRESSLIST,
                    timeout=duration * 60
                )
            )
            await self.send_message(room_id, 
                f"✅ Доступ открыт на {duration} мин.\n"
                f"   Ваш IP: {client_ip}\n"
                f"   Отключитесь: !disconnect"
            )
        except Exception as e:
            logger.error(f"RouterOS error: {e}", exc_info=True)
            self.state_manager.release(login)
            await self.send_message(room_id, f"❌ Ошибка настройки MikroTik: {str(e)}")

    async def _cmd_disconnect(self, room_id: str, login: str):
        """Логика отключения"""
        if not self.state_manager.is_user_connected(login):
            await self.send_message(room_id, "ℹ️ Вы не были подключены")
            return

        status = self.state_manager.get_user_status(login)
        if not status:
            await self.send_message(room_id, "❌ Ошибка получения статуса")
            return

        client_ip = status['computer_ip']
        try:
            await asyncio.to_thread(
                lambda: RouterOsClient(ROUTER_CONFIG).__enter__().remove_from_address_list(
                    address=client_ip,
                    list_name=config.ROUTEROS_ADDRESSLIST
                )
            )
            self.state_manager.release(login)
            await self.send_message(room_id, "✅ Отключен")
        except Exception as e:
            logger.error(f"RouterOS disconnect error: {e}", exc_info=True)
            await self.send_message(room_id, f"❌ Ошибка отключения: {str(e)}")

    async def _cmd_status(self, room_id: str, login: str):
        """Логика статуса"""
        connections = self.state_manager.get_user_connections()
        if not connections:
            await self.send_message(room_id, "ℹ️ Сейчас никто не подключен")
            return

        lines = []
        my_status = self.state_manager.get_user_status(login)
        if my_status:
            expires = datetime.fromisoformat(my_status['expires_at']).replace(tzinfo=timezone.utc)
            remaining = (expires - datetime.now(timezone.utc)).total_seconds() / 60
            lines.append(f"⏳ Ваше подключение: {my_status['computer_ip']} ({remaining:.1f} мин)")
        else:
            lines.append("ℹ️ Вы не подключены")

        lines.append("📊 Активные подключения:")
        for user, data in connections.items():
            expires = datetime.fromisoformat(data['expires_at']).replace(tzinfo=timezone.utc)
            remaining = (expires - datetime.now(timezone.utc)).total_seconds() / 60
            lines.append(f"   {user}: {data['computer_ip']} ({remaining:.1f} мин)")

        await self.send_message(room_id, "\n".join(lines))

    # ================= АДМИНСКИЕ КОМАНДЫ =================

    async def _cmd_admin_disconnect(self, room_id: str, admin_login: str, target: str):
        if not self.state_manager.is_user_connected(target):
            await self.send_message(room_id, f"ℹ️ Пользователь {target} не подключен")
            return

        status = self.state_manager.get_user_status(target)
        client_ip = status['computer_ip']
        try:
            await asyncio.to_thread(
                lambda: RouterOsClient(ROUTER_CONFIG).__enter__().remove_from_address_list(
                    address=client_ip,
                    list_name=config.ROUTEROS_ADDRESSLIST
                )
            )
            self.state_manager.release(target)
            await self.send_message(room_id, f"✅ Пользователь {target} отключен администратором {admin_login}")
        except Exception as e:
            await self.send_message(room_id, f"❌ Ошибка: {str(e)}")

    async def _cmd_check_user(self, room_id: str, target: str):
        with LdapSearcher() as ldap:
            in_group, raw = ldap.is_user_in_group(target, config.ALLOWED_USERS_GROUP) if config.ALLOWED_USERS_GROUP else (True, None)
            comp, _ = ldap.search_and_parse(search_filter=f'(&(objectClass=person)(sAMAccountName={target}))')
        
        lines = [f"👤 Пользователь: {target}",
                 f"📂 В группе '{config.ALLOWED_USERS_GROUP}': {'да' if in_group else 'нет'}",
                 f"💻 msDS-PrimaryComputer: {comp or 'Не указан'}"]
        await self.send_message(room_id, "\n".join(lines))

    async def _cmd_history(self, room_id: str, count: int):
        try:
            state = self.state_manager._read()
            full_history = state.get('history') or []
            history_slice = full_history[-count:]
            
            if not history_slice:
                await self.send_message(room_id, "ℹ️ История пуста")
                return

            lines = ["📜 ИСТОРИЯ ПОДКЛЮЧЕНИЙ:"]
            
            # 🛡️ Безопасное получение объекта таймзоны
            target_tz = getattr(config, 'TIMEZONE_TZINFO', None)
            if not isinstance(target_tz, timezone):
                target_tz = timezone.utc

            for item in reversed(history_slice):
                try:
                    reserved_at_str = item.get('reserved_at', 'Unknown')
                    user = item.get('user_login', '?')
                    ip = item.get('computer_ip', '?')
                    duration = item.get('duration_minutes', '?')
                    
                    if reserved_at_str != 'Unknown':
                        dt_obj = datetime.fromisoformat(reserved_at_str)
                        # Если время без зоны (naive), считаем что это UTC
                        if dt_obj.tzinfo is None:
                            dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                        # Конвертируем в нужную зону
                        time_str = dt_obj.astimezone(target_tz).strftime('%Y-%m-%d %H:%M')
                    else:
                        time_str = reserved_at_str

                    lines.append(f"  {time_str} | {user} | {ip} | {duration} мин")
                except Exception as e:
                    logger.warning(f"Skipped bad history record: {e}")
                    continue
            
            await self.send_message(room_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"History error: {e}", exc_info=True)
            await self.send_message(room_id, f"❌ Ошибка истории: {str(e)}")

    async def _cmd_log(self, room_id: str, count: int):
        log_path = getattr(config, 'LOG_FILE', '/app/logs/bot.log')
        try:
            dq = deque(maxlen=count)
            with open(log_path, 'rb') as f:
                for line in f:
                    dq.append(line.decode('utf-8', errors='replace').rstrip())
            
            if not dq:
                await self.send_message(room_id, f"ℹ️ Лог пуст")
                return
            
            payload = f"📄 Последние {len(dq)} строк лога:\n" + "\n".join(dq)
            # Matrix имеет лимит на размер сообщения, разбиваем если нужно
            chunk_size = 3000
            if len(payload) <= chunk_size:
                await self.send_message(room_id, payload)
            else:
                for i in range(0, len(payload), chunk_size):
                    await self.send_message(room_id, payload[i:i+chunk_size])
        except Exception as e:
            await self.send_message(room_id, f"❌ Ошибка чтения лога: {e}")

    async def _cmd_pending_list(self, room_id: str):
        pending = self.state_manager.list_pending()
        if not pending:
            await self.send_message(room_id, "ℹ️ Список ожидающих пуст")
            return
        lines = ["⏳ Ожидающие пользователи:"]
        for p in reversed(pending):
            lines.append(f"  {p['added_at'][:19]} | {p['user_login']} | {p.get('reason', '')}")
        await self.send_message(room_id, "\n".join(lines))

    async def _cmd_pending_clear(self, room_id: str):
        self.state_manager.clear_pending()
        await self.send_message(room_id, "✅ Список ожидающих очищен")

    async def _cmd_rule_toggle(self, room_id: str, comment: str, enable: bool):
        try:
            def _toggle():
                with RouterOsClient(ROUTER_CONFIG) as r:
                    if enable: r.enable_filter_rule_by_comment(comment)
                    else: r.disable_filter_rule_by_comment(comment)
            await asyncio.to_thread(_toggle)
            action = "включены" if enable else "отключены"
            await self.send_message(room_id, f"✅ Правила '{comment}' {action}")
        except Exception as e:
            await self.send_message(room_id, f"❌ Ошибка: {e}")

    async def _cmd_restart_service(self, room_id: str):
        # try:
        #     from service.pc_manager import restart_service_from_config
        #     success, output = await asyncio.to_thread(restart_service_from_config)
        #     status = "✅" if success else "❌"
        #     await self.send_message(room_id, f"{status} Перезапуск службы: {output.strip()}")
        # except Exception as e:
        #     await self.send_message(room_id, f"❌ Ошибка: {e}")
        pass

    async def _cmd_help(self, room_id: str, is_admin: bool):
        base = """📋 **Доступные команды:**
`!connect` или `!connect <мин>` — получить доступ
`!disconnect` — отключиться
`!status` — проверить статус
`!help` — эта справка"""
        if is_admin:
            base += """

🔧 **Административные команды:**
`!admin_disconnect <user>` — отключить пользователя
`!check_user <user>` — проверить AD-атрибуты
`!history [N]` — последние подключения
`!log [N]` — последние строки лога
`!pending_list` / `!pending_clear` — управление ожидающими
`!rule_enable/disable [comment]` — управление правилами FW
`!restart_service` — перезапуск службы на Windows"""
        await self.send_message(room_id, base)

    # ================= ЯДРО БОТА =================

    async def handle_command(self, command: str, args: str, room_id: str, sender: str):
        login = self.extract_login(sender)
        is_admin = login in self.admin_usernames

        try:
            if command == "help":
                await self._cmd_help(room_id, is_admin)
            elif command == "ping":
                await self.send_message(room_id, f"pong! @{login}")
            elif command == "connect":
                duration = int(args) if args.strip().isdigit() else 15
                await self._cmd_connect(room_id, login, duration)
            elif command == "disconnect":
                await self._cmd_disconnect(room_id, login)
            elif command == "status":
                await self._cmd_status(room_id, login)
            # Админские команды
            elif is_admin:
                if command == "admin_disconnect":
                    await self._cmd_admin_disconnect(room_id, login, args.strip())
                elif command == "check_user":
                    await self._cmd_check_user(room_id, args.strip())
                elif command == "history":
                    count = int(args) if args.strip().isdigit() else 20
                    await self._cmd_history(room_id, count)
                elif command == "log":
                    count = int(args) if args.strip().isdigit() else 20
                    await self._cmd_log(room_id, count)
                elif command == "pending_list":
                    await self._cmd_pending_list(room_id)
                elif command == "pending_clear":
                    await self._cmd_pending_clear(room_id)
                elif command == "rule_enable":
                    await self._cmd_rule_toggle(room_id, args.strip(), True)
                elif command == "rule_disable":
                    await self._cmd_rule_toggle(room_id, args.strip(), False)
                elif command == "restart_service":
                    await self._cmd_restart_service(room_id)
                else:
                    await self.send_message(room_id, "❓ Неизвестная команда. Напишите !help")
            else:
                await self.send_message(room_id, "❓ Неизвестная команда. Напишите !help")
                
        except Exception as e:
            logger.error(f"Command error: {e}", exc_info=True)
            await self.send_message(room_id, f"❌ Ошибка выполнения: {str(e)}")

    async def message_handler(self, room_id: str, event: RoomMessageText):
        sender = event.sender
        body = event.body.strip()
        
        if sender == self.client.user_id:
            return

        login = self.extract_login(sender)
        logger.info(f"Message from {login} in {room_id}: {body}")

        # Проверка прав доступа перед обработкой
        is_allowed = await self.is_user_allowed(login)
        
        if not is_allowed:
            await self.send_message(room_id, f"⛔ Доступ запрещен.\n\nПользователь @{login} не состоит в группе '{config.ALLOWED_USERS_GROUP}'.\nОбратитесь к администратору.")
            return

        match = re.match(r'^!(\w+)(?:\s+(.*))?$', body)
        if match:
            command = match.group(1).lower()
            args = match.group(2) or ""
            await self.handle_command(command, args, room_id, sender)
        else:
            # Игнорируем обычные сообщения
            pass

    async def handle_invite(self, room_id: str):
        logger.info(f"📩 Принимаю приглашение в {room_id}")
        try:
            await self.client.join(room_id)
            await self.send_message(room_id, "🤖 Привет! Я бот для VPN доступа.\nНапишите !help чтобы начать.")
        except Exception as e:
            logger.error(f"Failed to join room {room_id}: {e}")

    async def run(self):
        if not await self.login():
            return
        
        logger.info("👂 Бот слушает сообщения...")
        sync_token = None
        
        while True:
            try:
                response = await self.client.sync(timeout=30000, since=sync_token)
                
                if not isinstance(response, SyncResponse):
                    await asyncio.sleep(5)
                    continue
                
                sync_token = response.next_batch

                # 1. Обработка приглашений (Личных сообщений)
                for invite_id in response.rooms.invite.keys():
                    await self.handle_invite(invite_id)

                # 2. Обработка сообщений
                for room_id, room_data in response.rooms.join.items():
                    for event in room_data.timeline.events:
                        if isinstance(event, RoomMessageText):
                            await self.message_handler(room_id, event)

            except Exception as e:
                logger.error(f"❌ Ошибка в цикле синхронизации: {e}")
                await asyncio.sleep(5)

    async def close(self):
        await self.client.close()

async def main():
    bot = MatrixVPNBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("\n🛑 Остановка бота...")
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())