"""
Сервис для управления Asterisk через AMI с очередью и retry.
"""
import asyncio
import logging
import json
from panoramisk import Manager
from app.core.config import asterisk_config, AsteriskNode
from app.core.subscribers import Subscriber
from app.core.utils import get_play_count
import redis.asyncio as redis
import random
import re
from typing import List, Dict, Optional
from pathlib import Path

logger = logging.getLogger("voice-api.ami")

MAX_RETRIES = 2
RETRY_DELAY = 60  # секунд

# 

class AsteriskManagerService:
    """Управляет подключениями к Asterisk-узлам и инициирует звонки с очередью"""
    
    def __init__(self):
        self.managers = {}
        self.connecting = {}
        self.active_workers = {}  # campaign_id -> worker task
    
    async def _connect_node(self, node: AsteriskNode) -> Manager:
        if node.id in self.managers:
            return self.managers[node.id]
        
        if node.id not in self.connecting:
            self.connecting[node.id] = asyncio.Lock()
        
        async with self.connecting[node.id]:
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
        node = next((n for n in asterisk_config.nodes if n.id == node_id), None)
        if not node:
            raise ValueError(f"Node '{node_id}' not found in config")
        if not node.active:
            raise ValueError(f"Node '{node_id}' is inactive")
        return await self._connect_node(node)
    
    async def connect_all(self):
        logger.info("🌐 Eager-connecting to all Asterisk nodes...")
        for node in asterisk_config.nodes:
            if node.active:
                try:
                    await self._connect_node(node)
                except Exception as e:
                    logger.warning(f"⚠️ Node {node.id} unavailable at startup: {e}")
    
    def is_internal_number(self, node: AsteriskNode, number: str) -> bool:
        return (
            number.startswith(node.internal_prefix) and
            len(number) <= node.internal_max_length and
            number.isdigit()
        )
    
    def resolve_channel(self, node: AsteriskNode, number: str) -> tuple[str, str]:
        for rule in node.routing_rules:
            if re.match(rule.pattern, number):
                if rule.channel == "trunk":
                    if not node.external_trunks:
                        raise ValueError(f"No external trunks configured for number {number}")
                    trunk = random.choice(node.external_trunks)
                    return f"{trunk}/{number}", "trunk"
                else:
                    return f"{rule.channel}/{number}", rule.channel
        
        if len(number) <= node.internal_max_length and number.startswith(node.internal_prefix):
            return f"SIP/{number}", "SIP"
        else:
            if not node.external_trunks:
                raise ValueError(f"No external trunks configured for number {number}")
            trunk = random.choice(node.external_trunks)
            return f"{trunk}/{number}", "trunk"
    
    async def _get_trunk_load(self, redis_client: redis.Redis, trunk_name: str) -> int:
        """Получить текущую загрузку транка"""
        trunk_key = f"voice:active_trunk:{trunk_name}"
        active = await redis_client.get(trunk_key) or 0
        return int(active)
    
    async def _get_available_slots(self, node: AsteriskNode, redis_client: redis.Redis) -> Dict[str, int]:
        """Получить доступные слоты по каждому транку"""
        available = {}
        for trunk in node.external_trunks:
            load = await self._get_trunk_load(redis_client, trunk)
            slots = max(0, node.max_concurrent_per_trunk - load)
            if slots > 0:
                available[trunk] = slots
        return available
    
    async def originate_call(
        self,
        node_id: str,
        subscriber: Subscriber,
        audio_file: str,
        initiator: str = "Voice API",
        target_exten: str = "unknown",
        redis_client: redis.Redis = None,
        campaign_id: str = None,
        retry_count: int = 0
    ) -> dict:
        """Инициировать звонок с поддержкой retry"""
        manager = await self.get_manager(node_id)
        node = next(n for n in asterisk_config.nodes if n.id == node_id)
        play_count = get_play_count(subscriber.number)

        try:
            dial_string, channel_type = self.resolve_channel(node, subscriber.number)
        except ValueError as e:
            return {
                "status": "error",
                "subscriber": subscriber.display_name,
                "error": str(e),
                "campaign_id": campaign_id,
                "retry_count": retry_count
            }

        logger.info(
            f"📞 Originating call to {subscriber.display_name} via {dial_string} "
            f"(type: {channel_type}, retry: {retry_count})"
        )

        # 🔥 Имя транка считаем ОДИН раз (для внешних) или "internal"
        trunk_name = dial_string.rsplit("/", 1)[0] if channel_type == "trunk" else "internal"

        # 🔥 СТРАХОВКА ОТ ДВОЙНОГО ЗВОНКА:
        # один номер не может быть вызван дважды в одной кампании
        if redis_client and campaign_id:
            dedup_key = f"voice:dedup:{campaign_id}:{subscriber.number}"
            if not await redis_client.set(dedup_key, "1", nx=True, ex=600):
                logger.warning(
                    f"⚠️ Duplicate call prevented: {subscriber.number} "
                    f"in campaign {campaign_id}"
                )
                return {
                    "status": "skipped",
                    "subscriber": subscriber.display_name,
                    "error": "duplicate call blocked",
                    "campaign_id": campaign_id,
                    "retry_count": retry_count
                }

        # Проверка лимита для внешних транков
        if channel_type == "trunk" and redis_client:
            trunk_key = f"voice:active_trunk:{trunk_name}"
            active = await redis_client.get(trunk_key) or 0
            active = int(active)

            if active >= node.max_concurrent_per_trunk:
                logger.warning(
                    f"⚠️ Trunk {trunk_name} at max capacity "
                    f"({active}/{node.max_concurrent_per_trunk})"
                )
                return {
                    "status": "error",
                    "subscriber": subscriber.display_name,
                    "error": f"Trunk {trunk_name} at max capacity",
                    "campaign_id": campaign_id,
                    "retry_count": retry_count,
                    "retryable": True
                }

            await redis_client.incr(trunk_key)
            await redis_client.expire(trunk_key, 3600)

        # 🔥 ВАЖНО: variables присваивается ВСЕГДА, для ЛЮБОГО типа канала.
        # Этот блок должен быть на том же уровне отступа, что и if выше!
        variables = (
            f"TARGET_NUMBER={subscriber.number},"
            f"AUDIO_FILE={audio_file},"
            f"INITIATOR={initiator},"
            f"TARGET_EXTEN={target_exten},"
            f"ALERT_CALLERID={node.caller_id},"
            f"DIAL_STRING={dial_string},"
            f"CHANNEL_TYPE={channel_type},"
            f"EXTERNAL_TRUNK={trunk_name},"
            f"CAMPAIGN_ID={campaign_id or 'unknown'}",
            f"PLAY_COUNT={play_count}"
        )

        try:
            responses = await manager.send_action({
                'Action': 'Originate',
                'Channel': 'Local/s@pa_call_file_new/n',
                # 'Context': 'pa_call_file_new',
                'Context': 'alert_anchor',
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
                    "campaign_id": campaign_id,
                    "retry_count": retry_count
                }
            else:
                error_msg = responses.get('Message', 'Unknown error') if hasattr(responses, 'get') else str(responses)
                logger.error(f"❌ Failed to queue call to {subscriber.display_name}: {error_msg}")

                if channel_type == "trunk" and redis_client:
                    await redis_client.decr(f"voice:active_trunk:{trunk_name}")

                return {
                    "status": "error",
                    "subscriber": subscriber.display_name,
                    "error": error_msg,
                    "campaign_id": campaign_id,
                    "retry_count": retry_count,
                    "retryable": True
                }

        except Exception as e:
            logger.error(f"❌ Exception originating call to {subscriber.display_name}: {e}")

            if channel_type == "trunk" and redis_client:
                await redis_client.decr(f"voice:active_trunk:{trunk_name}")

            return {
                "status": "error",
                "subscriber": subscriber.display_name,
                "error": str(e),
                "campaign_id": campaign_id,
                "retry_count": retry_count,
                "retryable": True
            }
    
    async def get_trunks_health(self, redis_client=None) -> dict:
        """
        🩺 Здоровье узлов Asterisk и транков:
        - AMI-пинг по каждому активному узлу
        - занятость транков по счётчикам Redis
        """
        nodes_out = []

        for node in asterisk_config.nodes:
            if not node.active:
                continue

            # ── AMI ping ──
            ami_ok = True
            try:
                manager = await self.get_manager(node.id)
                await asyncio.wait_for(
                    manager.send_action({'Action': 'Ping'}),
                    timeout=3
                )
            except Exception as e:
                logger.warning(f"⚠️ AMI ping failed for {node.id}: {e}")
                ami_ok = False

            # ── Транки ──
            trunks_out = []
            for trunk in node.external_trunks:
                active = 0
                try:
                    if redis_client:
                        active = int(await redis_client.get(f"voice:active_trunk:{trunk}") or 0)
                except Exception:
                    active = -1

                trunks_out.append({
                    "name": trunk,
                    "active": active,
                    "max": node.max_concurrent_per_trunk,
                    "status": "full" if active >= node.max_concurrent_per_trunk else "ok"
                })

            nodes_out.append({
                "id": node.id,
                "name": node.name,
                "ami": "ok" if ami_ok else "down",
                "trunks": trunks_out
            })

        return {"nodes": nodes_out}

    
    async def _campaign_queue_worker(
        self,
        campaign_id: str,
        node_id: str,
        audio_file: str,
        initiator: str,
        redis_client: redis.Redis
    ):
        """Фоновый worker для обработки очереди внешних звонков"""
        logger.info(f"🔄 Starting queue worker for campaign {campaign_id}")
        
        node = next(n for n in asterisk_config.nodes if n.id == node_id), None
        if not node:
            logger.error(f"❌ Node {node_id} not found")
            return
        
        max_iterations = 1000  # Защита от бесконечного цикла
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            # Проверяем очередь
            queue_key = f"voice:queue:{campaign_id}"
            queue_len = await redis_client.llen(queue_key)
            
            if queue_len == 0:
                logger.info(f"✅ Queue empty for campaign {campaign_id}")
                break
            
            # Получаем доступные слоты
            available_slots = await self._get_available_slots(node, redis_client)
            total_available = sum(available_slots.values())
            
            if total_available == 0:
                # Ждем освобождения слотов
                await asyncio.sleep(2)
                continue
            
            # Берем номер из очереди (round-robin по транкам)
            sub_data = await redis_client.rpop(queue_key)
            if not sub_data:
                break
            
            try:
                sub_dict = json.loads(sub_data)
                subscriber = Subscriber(**sub_dict)
            except Exception as e:
                logger.error(f"❌ Failed to parse subscriber data: {e}")
                continue
            
            # Выбираем транк с наименьшей загрузкой
            selected_trunk = min(available_slots.keys(), key=lambda t: available_slots[t])
            
            # Пытаемся позвонить
            result = await self.originate_call(
                node_id=node_id,
                subscriber=subscriber,
                audio_file=audio_file,
                initiator=initiator,
                redis_client=redis_client,
                campaign_id=campaign_id,
                retry_count=sub_dict.get("retry_count", 0)
            )
            
            # Обрабатываем retry
            if result["status"] == "error" and result.get("retryable"):
                retry_count = result.get("retry_count", 0)
                if retry_count < MAX_RETRIES:
                    sub_dict["retry_count"] = retry_count + 1
                    retry_queue_key = f"voice:retry:{campaign_id}"
                    await redis_client.lpush(retry_queue_key, json.dumps(sub_dict))
                    logger.info(f"🔄 Queued retry for {subscriber.display_name} (attempt {retry_count + 1}/{MAX_RETRIES})")
                else:
                    logger.error(f"❌ Max retries reached for {subscriber.display_name}")
            
            # Небольшая задержка между звонками
            await asyncio.sleep(0.5)
        
        # Обрабатываем retry-очередь
        retry_queue_key = f"voice:retry:{campaign_id}"
        retry_len = await redis_client.llen(retry_queue_key)
        
        if retry_len > 0:
            logger.info(f"⏳ Waiting {RETRY_DELAY}s before processing {retry_len} retries")
            await asyncio.sleep(RETRY_DELAY)
            
            # Перемещаем retry в основную очередь
            while True:
                retry_data = await redis_client.rpop(retry_queue_key)
                if not retry_data:
                    break
                await redis_client.lpush(queue_key, retry_data)
            
            # Перезапускаем worker
            asyncio.create_task(self._campaign_queue_worker(
                campaign_id, node_id, audio_file, initiator, redis_client
            ))
        
        logger.info(f"✅ Queue worker finished for campaign {campaign_id}")
    
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
        
        # 🔥 ПРАВИЛЬНОЕ получение объекта node
        node = None
        for n in asterisk_config.nodes:
            if n.id == node_id:
                node = n
                break
        
        if not node:
            logger.error(f"❌ Node '{node_id}' not found in config")
            return {"status": "error", "error": f"Node '{node_id}' not found"}
        
        # Создаем задачи для всех абонентов
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
        
        # Ждём завершения всех задач
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
    
    async def get_campaign_status(self, campaign_id: str, redis_client: redis.Redis) -> dict:
        """Получить статус кампании"""
        queue_key = f"voice:queue:{campaign_id}"
        retry_key = f"voice:retry:{campaign_id}"
        
        queue_len = await redis_client.llen(queue_key)
        retry_len = await redis_client.llen(retry_key)
        
        return {
            "campaign_id": campaign_id,
            "queued": queue_len,
            "retrying": retry_len,
            "worker_active": campaign_id in self.active_workers
        }

# Глобальный инстанс
asterisk_manager = AsteriskManagerService()