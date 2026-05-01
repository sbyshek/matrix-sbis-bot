import asyncio
import logging
from nio import AsyncClient, SyncResponse, RoomMessageText, LoginResponse
import config
from service.registry_gen import generate_registry
from token_manager import load_token, save_token
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class RegistryBot:
    def __init__(self):
        logging.info("🚀 Запуск RegistryBot...")
        logging.info(f"👤 Пользователь: {config.USERNAME}")
        # logger.info(f"🏠 Пароль: {config.PASSWORD}")
        self.client = AsyncClient(config.HOMESERVER, config.USERNAME)

    async def login(self):
        resp = await self.client.login(config.PASSWORD)
        if isinstance(resp, LoginResponse):
            logger.info("✅ RegistryBot logged in")
            return True
        logger.error(f"❌ Login failed: {resp}")
        return False

    async def send(self, room: str, text: str):
        await self.client.room_send(room, "m.room.message", {"msgtype": "m.text", "body": text})

    async def handle_command(self, room: str, sender: str, args: str):
        if not args:
            await self.send(room, "❓ Использование: `!registry N:\\Путь\\К\\Папке`")
            return

        target_path = args.strip()
        await self.send(room, f"🔍 Начинаю формирование реестра для: `{target_path}`\n Подождите...")

        try:
            # Блокирующие SMB/PDF/Excel операции выносим в пул потоков
            fname, result_msg = await asyncio.to_thread(generate_registry, target_path)
            await self.send(room, result_msg)
        except ValueError as e:
            await self.send(room, f"❌ Ошибка ввода: {e}")
        except Exception as e:
            logger.error(f"Registry generation failed: {e}", exc_info=True)
            await self.send(room, f"❌ Ошибка генерации: {str(e)}")

    async def message_handler(self, room: str, event: RoomMessageText):
        sender = event.sender
        if sender == self.client.user_id:
            return

        body = event.body.strip()
        if body.startswith("!registry") or body.startswith("!реестр"):
            args = body.split(maxsplit=1)[1] if len(body.split()) > 1 else ""
            await self.handle_command(room, sender, args)
        elif body.lower() == "!help":
            await self.send(room, "📋 **Команды:**\n`!registry N:\\Путь` — создать реестр документов в папке")

    # async def run(self):
    #     if not await self.login(): return
    #     logger.info("👂 RegistryBot listening...")
    #     token = None
    #     while True:
    #         try:
    #             resp = await self.client.sync(timeout=30000, since=token)
    #             if not isinstance(resp, SyncResponse):
    #                 await asyncio.sleep(5); continue
    #             token = resp.next_batch

    #             for rid in resp.rooms.invite.keys():
    #                 await self.client.join(rid)
    #                 await self.send(rid, "📄 Привет! Я бот для создания реестров документов.\nНапишите `!help`")

    #             for rid, rdata in resp.rooms.join.items():
    #                 for ev in rdata.timeline.events:
    #                     if isinstance(ev, RoomMessageText):
    #                         await self.message_handler(rid, ev)
    #         except Exception as e:
    #             logger.error(f"Sync error: {e}"); await asyncio.sleep(5)
    
    
    async def run(self):
        if not await self.login(): return
        
        # Загружаем последний токен синхронизации
        token = load_token()
        logger.info(f"👂 Bot listening... (token: {token or 'None'})")
        
        while True:
            try:
                resp = await self.client.sync(timeout=30000, since=token)
                if not isinstance(resp, SyncResponse):
                    await asyncio.sleep(5); continue
                
                # ✅ Сохраняем новый токен сразу
                token = resp.next_batch
                save_token(token)
                
                now_ms = int(time.time() * 1000)
                
                for rid, rdata in resp.rooms.join.items():
                    for ev in rdata.timeline.events:
                        if isinstance(ev, RoomMessageText):
                            # ⏱️ Игнорируем сообщения, отправленные >60 сек назад
                            if hasattr(ev, 'server_timestamp') and (now_ms - ev.server_timestamp > 60000):
                                continue
                            await self.message_handler(rid, ev)
                            
            except Exception as e:
                logger.error(f"Sync error: {e}"); await asyncio.sleep(5)

    async def close(self):
        await self.client.close()

async def main():
    bot = RegistryBot()
    try: await bot.run()
    except KeyboardInterrupt: logger.info("🛑 Stop"); await bot.close()

if __name__ == "__main__":
    asyncio.run(main())