# app/services/asterisk_manager.py
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
import time
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
    
    
    async def _trunk_has_capacity(self, redis_client, node, trunk: str) -> bool:
        """
        Есть ли ёмкость на транке.
        Fail-open: если сверка протухла (>30с) — разрешаем звонок.
        Источник истины — Астериск, а не наша книга учёта.
        """
        ts_raw = await redis_client.get(f"voice:trunk_reconcile:{node.id}")
        if not ts_raw:
            return True                      # сверки ещё не было → не блокируем

        loop = asyncio.get_event_loop()
        age = loop.time() - float(ts_raw)
        if age > 30:
            logger.warning(f"⚠️ Reconcile stale ({age:.0f}s) → fail-open for {trunk}")
            return True                      # данные протухли → шлагбаум ОТКРЫТ

        active = int(await redis_client.get(f"voice:active_trunk:{trunk}") or 0)
        return active < node.max_concurrent_per_trunk
    
    
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
        retry_count: int = 0,
        trunk_override=None
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
        
        if trunk_override and channel_type == "trunk":
                dial_string = f"{trunk_override}/{subscriber.number}"


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
    
    

    
    @staticmethod
    def _sip_peer_state(status: str) -> str:
        """Нормализация статуса SIP-пира из AMI SIPpeers."""
        s = (status or "").strip().lower()
        if not s:
            return "unknown"
        if s.startswith("ok") or "registered" in s:
            return "online"
        if any(x in s for x in ("unreachable", "unregistered", "rejected", "forbidden", "not exists")):
            return "offline"
        return "unknown"

    async def get_trunks_health(self, redis_client=None) -> dict:
        """
        🩺 Здоровье узлов и транков:
        - AMI-пинг
        - регистрация SIP-пиров транков (CLI sip show peers)
        - занятость по счётчикам Redis
        """
        nodes_out = []

        for node in asterisk_config.nodes:
            if not node.active:
                continue

            # ── AMI ping ──
            ami_ok = True
            manager = None
            try:
                manager = await self.get_manager(node.id)
                await asyncio.wait_for(manager.send_action({'Action': 'Ping'}), timeout=3)
            except Exception as e:
                logger.warning(f"⚠️ AMI ping failed for {node.id}: {e}")
                ami_ok = False

            # ── Статусы SIP-пиров через CLI ──
            peer_statuses = {}
            if ami_ok and manager:
                needed = {
                    (t.split('/', 1)[1] if t.startswith('SIP/') else t)
                    for t in node.external_trunks
                }
                try:
                    msg = await asyncio.wait_for(
                        manager.send_action({'Action': 'Command', 'Command': 'sip show peers'}),
                        timeout=5
                    )
                    out = msg.get('Output') or ''
                    # 🔥 panoramisk возвращает Output как LIST строк — склеиваем
                    if isinstance(out, (list, tuple)):
                        out = '\n'.join(out)

                    for line in str(out).splitlines():
                        parts = line.split()
                        if not parts:
                            continue
                        name = parts[0].split('/')[0]
                        if name in needed:
                            # peer_statuses[name] = parts[-1]
                            
                            # Стандартные колонки: 0:Name, 1:Host, 2:Dynamic, 3:Forcerport, 4:Port, 5+:Status
                            # Собираем всё с 5-го индекса, чтобы получить "OK (10 ms)" или "UNREACHABLE" целиком
                            peer_statuses[name] = ' '.join(parts[5:]) if len(parts) >= 6 else ' '.join(parts[1:])
                except Exception as e:
                    logger.warning(f"⚠️ CLI sip show peers failed for {node.id}: {e}")

            # ── Транки ──
            trunks_out = []
            for trunk in node.external_trunks:
                active = 0
                try:
                    if redis_client:
                        active = int(await redis_client.get(f"voice:active_trunk:{trunk}") or 0)
                except Exception:
                    active = -1

                peer_name = trunk.split('/', 1)[1] if trunk.startswith('SIP/') else trunk
                raw_status = peer_statuses.get(peer_name, '').lower()

                if 'unreachable' in raw_status or 'unregistered' in raw_status:
                    reg = 'offline'
                elif 'ok' in raw_status or 'registered' in raw_status:
                    reg = 'online'
                else:
                    reg = 'unknown'

                if reg == 'offline':
                    status = 'offline'
                elif active >= node.max_concurrent_per_trunk:
                    status = 'full'
                elif active > 0:
                    status = 'busy'
                else:
                    status = 'ok' if reg == 'online' else 'unknown'

                trunks_out.append({
                    "name": trunk,
                    "active": active,
                    "max": node.max_concurrent_per_trunk,
                    "registration": reg,
                    "status": status,
                    "raw": raw_status
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
    
    
    
    # async def originate_campaign(self, node_id, subscribers, audio_file,
    #                              initiator="Voice API", target_exten="unknown",
    #                              redis_client=None, campaign_id=None) -> dict:
    #     await redis_client.hset(f"voice:campaign:{campaign_id}", mapping={
    #         "node_id": node_id, "audio_file": audio_file, "initiator": initiator,
    #     })
    #     await redis_client.expire(f"voice:campaign:{campaign_id}", 7200)

    #     node = next((n for n in asterisk_config.nodes if n.id == node_id), None)
    #     if not node:
    #         return {"status": "error", "error": f"Node '{node_id}' not found"}

    #     tasks, queued = [], 0
    #     for subscriber in subscribers:
    #         try:
    #             _, ch_type = self.resolve_channel(node, subscriber.number)
    #         except ValueError:
    #             ch_type = "trunk"
    #         if ch_type == "trunk":
    #             # 🔥 внешние НЕ шлём залпом — в общую очередь с пейсингом
    #             await redis_client.zadd("voice:ext_queue", {json.dumps({
    #                 "cid": campaign_id,
    #                 "number": subscriber.number,
    #                 "name": subscriber.display_name,
    #             }): time.time()})
    #             queued += 1
    #         else:
    #             tasks.append(asyncio.create_task(self.originate_call(
    #                 node_id=node_id, subscriber=subscriber, audio_file=audio_file,
    #                 initiator=initiator, target_exten=target_exten,
    #                 redis_client=redis_client, campaign_id=campaign_id)))

    #     results_list = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    #     results = {"success": queued, "error": 0, "queued_external": queued, "details": []}
    #     for r in results_list:
    #         if isinstance(r, Exception):
    #             results["error"] += 1; results["details"].append({"status": "error", "error": str(r)})
    #         elif r.get("status") == "success":
    #             results["success"] += 1; results["details"].append(r)
    #         else:
    #             results["error"] += 1; results["details"].append(r)
    #     logger.info(f"📊 Campaign {campaign_id[:8]}: internals done, external queued={queued}")
    #     return results
    
    
    async def originate_campaign(self, node_id, subscribers, audio_file,
                                 initiator="Voice API", target_exten="unknown",
                                 redis_client=None, campaign_id=None) -> dict:
        await redis_client.hset(f"voice:campaign:{campaign_id}", mapping={
            "node_id": node_id, "audio_file": audio_file, "initiator": initiator,
            "target_exten": target_exten,
        })
        await redis_client.expire(f"voice:campaign:{campaign_id}", 7200)

        node = next((n for n in asterisk_config.nodes if n.id == node_id), None)
        if not node:
            return {"status": "error", "error": f"Node '{node_id}' not found"}

        int_queued = 0
        ext_queued = 0

        for subscriber in subscribers:
            try:
                _, ch_type = self.resolve_channel(node, subscriber.number)
            except ValueError:
                ch_type = "trunk"

            member = json.dumps({
                "cid": campaign_id,
                "number": subscriber.number,
                "name": subscriber.display_name,
            }, ensure_ascii=False)

            if ch_type == "trunk":
                await redis_client.zadd("voice:ext_queue", {member: time.time()})
                ext_queued += 1
            else:
                # 🔥 Внутренние тоже в очередь с пейсингом
                await redis_client.zadd("voice:int_queue", {member: time.time()})
                int_queued += 1

        logger.info(
            f"📊 Campaign {campaign_id[:8]}: "
            f"queued int={int_queued}, ext={ext_queued}"
        )
        return {
            "status": "queued",
            "queued_internal": int_queued,
            "queued_external": ext_queued,
            "total": len(subscribers),
        }
    
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
        
    async def reconcile_trunk_counters(self, redis_client) -> int:
        """
        🔥 Источник истины — живые каналы Asterisk.
        Перезаписываем счётчики фактическим количеством каналов на транк.
        Залипание становится невозможным дольше цикла сверки.
        """
        updated = 0
        for node in asterisk_config.nodes:
            if not node.active:
                continue
            try:
                manager = await self.get_manager(node.id)
                msg = await asyncio.wait_for(
                    manager.send_action({'Action': 'Command',
                                        'Command': 'core show channels concise'}),
                    timeout=5
                )
                out = msg.get('Output') or ''
                if isinstance(out, (list, tuple)):
                    out = '\n'.join(out)

                counts = {t: 0 for t in node.external_trunks}
                for line in out.splitlines():
                    chan = line.split('|')[0].strip() if '|' in line else (line.split() or [''])[0]
                    for trunk in counts:
                        # каналы вида SIP/rt_3049616-0005027c
                        if chan.startswith(trunk + '-') or chan.startswith(trunk + '/'):
                            counts[trunk] += 1
                            break

                for trunk, cnt in counts.items():
                    await redis_client.set(f"voice:active_trunk:{trunk}", cnt)
                    updated += 1

                await redis_client.set(f"voice:trunk_reconcile:{node.id}",
                                    str(int(asyncio.get_event_loop().time())))
            except Exception as e:
                logger.warning(f"⚠️ Trunk reconcile failed for {node.id}: {e}")
        return updated
    
    
    async def get_live_load(self, node: AsteriskNode) -> dict:
        """
        Фактическая нагрузка узла из AMI (core show channels concise).
        Возвращает:
          { "trunks": {trunk_name: active_count, ...}, "internal": count }
        Источник истины — Asterisk, а не наша книга учёта.
        """
        manager = await self.get_manager(node.id)
        msg = await asyncio.wait_for(
            manager.send_action({'Action': 'Command',
                                 'Command': 'core show channels concise'}),
            timeout=5
        )
        out = msg.get('Output') or ''
        if isinstance(out, (list, tuple)):
            out = '\n'.join(out)

        trunk_load = {t: 0 for t in node.external_trunks}
        internal = 0
        for line in str(out).splitlines():
            chan = line.split('|')[0].strip() if '|' in line else (line.split() or [''])[0]
            if not chan.startswith('SIP/'):
                continue
            for t in trunk_load:
                if chan.startswith(t + '-') or chan.startswith(t + '/'):
                    trunk_load[t] += 1
                    break
            else:
                internal += 1
        return {"trunks": trunk_load, "internal": internal}

# Глобальный инстанс
asterisk_manager = AsteriskManagerService()