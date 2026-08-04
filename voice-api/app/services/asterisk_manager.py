"""
Сервис для управления Asterisk через AMI (Asterisk Manager Interface).
Использует библиотеку panoramisk для асинхронной работы.
"""

import asyncio
import logging
from panoramisk import Manager
from app.core.config import asterisk_config, AsteriskNode
from app.core.subscribers import Subscriber
import redis.asyncio as redis
import random
import re


    


logger = logging.getLogger("voice-api.ami")

class AsteriskManagerService:
    """Управляет подключениями к Asterisk-узлам и инициирует звонки"""
    
    def __init__(self):
        self.managers = {}  # node_id -> Manager instance
        self.connecting = {}  # node_id -> asyncio.Lock (защита от гонок)
    
    async def _connect_node(self, node: AsteriskNode) -> Manager:
        """Подключение к одному узлу с защитой от гонок"""
        if node.id in self.managers:
            return self.managers[node.id]
        
        # Блокировка, чтобы два одновременных запроса не создали два соединения
        if node.id not in self.connecting:
            self.connecting[node.id] = asyncio.Lock()
        
        async with self.connecting[node.id]:
            # Двойная проверка после захвата локи
            if node.id in self.managers:
                return self.managers[node.id]
            
            logger.info(f"🔗 Connecting to Asterisk node: {node.id} ({node.host}:{node.port})")
            try:
                manager = Manager(
                    loop=asyncio.get_event_loop(),
                    host=node.host,
                    port=node.port,
                    username=node.username,
                    secret=node.secret,
                    encoding='utf-8'
                )
                await manager.connect()
                self.managers[node.id] = manager
                logger.info(f"✅ Connected to {node.id} ({node.name})")
                return manager
            except Exception as e:
                logger.error(f"❌ Failed to connect to {node.id}: {e}")
                raise
    
    async def get_manager(self, node_id: str) -> Manager:
        """Получить менеджер узла (лениво + кэш)"""
        node = next((n for n in asterisk_config.nodes if n.id == node_id), None)
        if not node:
            raise ValueError(f"Node '{node_id}' not found in config")
        if not node.active:
            raise ValueError(f"Node '{node_id}' is inactive")
        
        return await self._connect_node(node)
    
    async def connect_all(self):
        """Eager-подключение всех узлов при старте"""
        logger.info("🌐 Eager-connecting to all Asterisk nodes...")
        for node in asterisk_config.nodes:
            if node.active:
                try:
                    await self._connect_node(node)
                except Exception as e:
                    logger.warning(f"⚠️ Node {node.id} unavailable at startup: {e}")
    
    def is_internal_number(self, node: AsteriskNode, number: str) -> bool:
        """
        Определяет, является ли номер внутренним.
        Критерии: начинается с internal_prefix И длина <= internal_max_length
        """
        return (
            number.startswith(node.internal_prefix) and 
            len(number) <= node.internal_max_length and
            number.isdigit()
        )
    
    def resolve_channel(self, node: AsteriskNode, number: str) -> tuple[str, str]:
        """
        Определяет тип канала для номера по правилам маршрутизации.
        
        Returns:
            (channel_string, channel_type_label)
            Например: ("SIP/4299", "SIP") или ("IAX2/office/4591", "IAX2")
        """
        # Сначала проверяем routing_rules из конфига
        for rule in node.routing_rules:
            if re.match(rule.pattern, number):
                if rule.channel == "trunk":
                    # Для внешних номеров выбираем случайный транк
                    if not node.external_trunks:
                        raise ValueError(f"No external trunks configured for number {number}")
                    trunk = random.choice(node.external_trunks)
                    return f"{trunk}/{number}", "trunk"
                else:
                    # Для внутренних (SIP, IAX2 и т.д.)
                    return f"{rule.channel}/{number}", rule.channel
        
        # Fallback: если ни одно правило не подошло
        if len(number) <= node.internal_max_length and number.startswith(node.internal_prefix):
            return f"SIP/{number}", "SIP"
        else:
            if not node.external_trunks:
                raise ValueError(f"No external trunks configured for number {number}")
            trunk = random.choice(node.external_trunks)
            return f"{trunk}/{number}", "trunk"
            
    # async def originate_call(
    #     self, 
    #     node_id: str, 
    #     subscriber: Subscriber,
    #     audio_file: str,
    #     initiator: str = "Voice API",
    #     target_exten: str = "unknown",
    #     redis_client: redis.Redis = None,
    #     campaign_id: str = None
    # ) -> dict:
    #     """Инициировать умный звонок через кастомный контекст FreePBX"""
    #     manager = await self.get_manager(node_id)
    #     node = next(n for n in asterisk_config.nodes if n.id == node_id)
        
    #     is_internal = self.is_internal_number(node, subscriber.number)
        
    #     if is_internal:
    #         channel = f"SIP/{subscriber.number}"
    #         selected_trunk = "internal"
    #         logger.info(f"📞 Originating internal call to {subscriber.display_name}")
    #     else:
    #         # 🔥 ВЫБОР ТРАНКА: Берем случайный из списка для балансировки
    #         if not node.external_trunks:
    #             return {"status": "error", "subscriber": subscriber.display_name, "error": "No external trunks configured"}
            
    #         selected_trunk = random.choice(node.external_trunks)
    #         channel = f"{selected_trunk}/{subscriber.number}"
    #         logger.info(f"📞 Originating external call to {subscriber.display_name} via trunk {selected_trunk}")
            
    #         # 🔥 ПРОВЕРКА ЛИМИТА НА ТРАНК
    #         trunk_key = f"voice:active_trunk:{selected_trunk}"
    #         active = await redis_client.get(trunk_key) or 0
    #         active = int(active)
            
    #         if active >= node.max_concurrent_per_trunk:
    #             logger.warning(f"⚠️ Trunk {selected_trunk} is at max capacity ({active}/{node.max_concurrent_per_trunk})")
    #             return {
    #                 "status": "error",
    #                 "subscriber": subscriber.display_name,
    #                 "error": f"Trunk {selected_trunk} is at max capacity"
    #             }
            
    #         # Увеличиваем счетчик и ставим TTL на всякий случай
    #         await redis_client.incr(trunk_key)
    #         await redis_client.expire(trunk_key, 3600)

    #     # Формируем строку переменных для передачи в Asterisk
    #     variables = (
    #         f"TARGET_NUMBER={subscriber.number},"
    #         f"AUDIO_FILE={audio_file},"
    #         f"INITIATOR={initiator},"
    #         f"TARGET_EXTEN={target_exten},"
    #         f"ALERT_CALLERID={node.caller_id},"
    #         f"EXTERNAL_TRUNK={selected_trunk}",  # 🔥 Передаем выбранный транк в Asterisk
    #         f"CAMPAIGN_ID={campaign_id}"
    #     )
        
    #     try:
    #         responses = await manager.send_action({
    #             'Action': 'Originate',
    #             'Channel': 'Local/s@pa_call_file_new',
    #             'Context': 'pa_call_file_new',
    #             'Exten': 's',
    #             'Priority': '1',
    #             'Variable': variables,
    #             'Async': 'true'
    #         })
            
    #         response_status = responses.get('Response', '') if hasattr(responses, 'get') else str(responses)
            
    #         if 'Success' in response_status:
    #             logger.info(f"✅ Successfully queued alert call to {subscriber.display_name}")
    #             return {
    #                 "status": "success",
    #                 "subscriber": subscriber.display_name,
    #                 "number": subscriber.number,
    #                 "type": "internal" if is_internal else "external",
    #                 "trunk": selected_trunk,
    #                 "node": node_id,
    #                 "campaign_id": campaign_id
    #             }
    #         else:
    #             error_msg = responses.get('Message', 'Unknown error') if hasattr(responses, 'get') else str(responses)
    #             logger.error(f"❌ Failed to queue call to {subscriber.display_name}: {error_msg}")
    #             # Если ошибка, уменьшаем счетчик транка
    #             if not is_internal:
    #                 await redis_client.decr(f"voice:active_trunk:{selected_trunk}")
    #             return {
    #                 "status": "error",
    #                 "subscriber": subscriber.display_name,
    #                 "error": error_msg,
    #                 "campaign_id": campaign_id
    #             }
                
    #     except Exception as e:
    #         logger.error(f"❌ Exception while originating call to {subscriber.display_name}: {e}")
    #         if not is_internal:
    #             await redis_client.decr(f"voice:active_trunk:{selected_trunk}")
    #         return {
    #             "status": "error",
    #             "subscriber": subscriber.display_name,
    #             "error": str(e)
    #         }


    async def originate_call(
        self, 
        node_id: str, 
        subscriber: Subscriber,
        audio_file: str,
        initiator: str = "Voice API",
        target_exten: str = "unknown",
        redis_client: redis.Redis = None,
        campaign_id: str = None
    ) -> dict:
        """Инициировать умный звонок через кастомный контекст FreePBX"""
        manager = await self.get_manager(node_id)
        node = next(n for n in asterisk_config.nodes if n.id == node_id)
        
        # 🔥 Определяем канал по правилам маршрутизации
        try:
            dial_string, channel_type = self.resolve_channel(node, subscriber.number)
        except ValueError as e:
            return {"status": "error", "subscriber": subscriber.display_name, "error": str(e), "campaign_id": campaign_id}
        
        logger.info(f"📞 Originating call to {subscriber.display_name} via {dial_string} (type: {channel_type})")
        
        # 🔥 Проверка лимита для внешних транков
        if channel_type == "trunk" and redis_client:
            trunk_name = dial_string.rsplit("/", 1)[0]  # Извлекаем имя транка из "SIP/rt_3049625/8923..."
            trunk_key = f"voice:active_trunk:{trunk_name}"
            active = await redis_client.get(trunk_key) or 0
            active = int(active)
            
            if active >= node.max_concurrent_per_trunk:
                logger.warning(f"⚠️ Trunk {trunk_name} at max capacity ({active}/{node.max_concurrent_per_trunk})")
                return {
                    "status": "error",
                    "subscriber": subscriber.display_name,
                    "error": f"Trunk {trunk_name} at max capacity",
                    "campaign_id": campaign_id
                }
            
            await redis_client.incr(trunk_key)
            await redis_client.expire(trunk_key, 3600)

        variables = (
            f"TARGET_NUMBER={subscriber.number},"
            f"AUDIO_FILE={audio_file},"
            f"INITIATOR={initiator},"
            f"TARGET_EXTEN={target_exten},"
            f"ALERT_CALLERID={node.caller_id},"
            f"DIAL_STRING={dial_string},"  # 🔥 Передаём готовую строку Dial в Asterisk
            f"CHANNEL_TYPE={channel_type},"
            f"CAMPAIGN_ID={campaign_id or 'unknown'}"
        )
        
        try:
            responses = await manager.send_action({
                'Action': 'Originate',
                'Channel': 'Local/s@pa_call_file_new/n',  # 🔥 ФЛАГ /n — УБИРАЕТ ДВОЙНОЙ ЗВОНОК!
                'Context': 'pa_call_file_new',
                'Exten': 's',
                'Priority': '1',
                'Variable': variables,
                'Async': 'true'
            })
            
            response_status = responses.get('Response', '') if hasattr(responses, 'get') else str(responses)
            
            if 'Success' in response_status:
                logger.info(f"✅ Queued call to {subscriber.display_name} via {dial_string}")
                return {
                    "status": "success",
                    "subscriber": subscriber.display_name,
                    "number": subscriber.number,
                    "channel": dial_string,
                    "type": channel_type,
                    "node": node_id,
                    "campaign_id": campaign_id
                }
            else:
                error_msg = responses.get('Message', 'Unknown error') if hasattr(responses, 'get') else str(responses)
                logger.error(f"❌ Failed to queue call to {subscriber.display_name}: {error_msg}")
                if channel_type == "trunk" and redis_client:
                    trunk_name = dial_string.rsplit("/", 1)[0]
                    await redis_client.decr(f"voice:active_trunk:{trunk_name}")
                return {
                    "status": "error",
                    "subscriber": subscriber.display_name,
                    "error": error_msg,
                    "campaign_id": campaign_id
                }
                
        except Exception as e:
            logger.error(f"❌ Exception originating call to {subscriber.display_name}: {e}")
            if channel_type == "trunk" and redis_client:
                trunk_name = dial_string.rsplit("/", 1)[0]
                await redis_client.decr(f"voice:active_trunk:{trunk_name}")
            return {
                "status": "error",
                "subscriber": subscriber.display_name,
                "error": str(e),
                "campaign_id": campaign_id
            }



    async def originate_campaign(
        self,
        node_id: str,
        subscribers: list[Subscriber],
        audio_file: str,
        initiator: str = "Voice API",
        target_exten: str = "unknown",
        redis_client: redis.Redis = None, 
        campaign_id: str = None
    ) -> dict:
        """Запустить кампанию обзвона МАКСИМАЛЬНО ПАРАЛЛЕЛЬНО"""
        
        # 🔥 Создаем задачи для всех абонентов
        tasks = []
        for subscriber in subscribers:
            task = asyncio.create_task(
                self.originate_call(
                    node_id=node_id,
                    subscriber=subscriber,
                    audio_file=audio_file,
                    initiator=initiator,
                    target_exten=target_exten,
                    redis_client=redis_client,
                    campaign_id=campaign_id
                )
            )
            tasks.append(task)
        
        # 🔥 Ждём завершения всех задач (но они уже запущены параллельно)
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Собираем статистику
        results = {"success": 0, "error": 0, "details": []}
        for result in results_list:
            if isinstance(result, Exception):
                results["error"] += 1
                results["details"].append({"status": "error", "error": str(result)})
            elif result.get("status") == "success":
                results["success"] += 1
                results["details"].append(result)
            else:
                results["error"] += 1
                results["details"].append(result)
        
        logger.info(
            f"📊 Campaign finished: {results['success']} success, "
            f"{results['error']} errors out of {len(subscribers)} subscribers"
        )
        
        return results

# Глобальный инстанс
asterisk_manager = AsteriskManagerService()