import asyncio
import random
import json
import logging
from urllib.parse import urljoin
from aiohttp import ClientSession, ClientTimeout
from redis import asyncio as aioredis
from pydantic import BaseModel, Field
import config 

logger = logging.getLogger(__name__)

class VehicleData(BaseModel):
    vehicleId: int = Field(0, alias="vehicleId")
    # ✅ ИСПРАВЛЕНО: API использует 'name' для гос. номера
    vehicleNumber: str = Field("", alias="name")
    latitude: float = Field(None, alias="latitude")
    longitude: float = Field(None, alias="longitude")
    address: str = Field("", alias="address")
    
    # Дополнительные поля из /find (опционально)
    modelName: str = Field("", alias="modelName")
    status: int = Field(0, alias="status")
    
    def get_redis_key(self):
        return f"glonass:vehicle:{self.vehicleId}"
class GlonassClient:
    def __init__(self):
        self.url = config.GLONASS_URL
        self.login = config.GLONASS_LOGIN
        self.password = config.GLONASS_PASSWORD
        self.timeout = ClientTimeout(total=10) # Уменьшил таймаут, чтобы не висло
        self.headers = {
            "User-Agent": "AutoBot/1.0",
            "Content-Type": "application/json"
        }
        self.auth_id = None
        self.redis = None
        self.cache_prefix = "glonass:"

    async def _get_redis(self):
        if self.redis is None:
            try:
                # Подключаемся к Redis
                self.redis = await aioredis.from_url(config.REDIS_URL, decode_responses=True)
                await self.redis.ping() # Проверка связи
            except Exception as e:
                logger.warning(f"⚠️ Redis недоступен: {e}. Работаю без кэша.")
                return None
        return self.redis

    async def _ensure_auth(self):
        # 1. Пробуем взять токен из Redis
        redis = await self._get_redis()
        if redis:
            try:
                cached = await redis.get(f"{self.cache_prefix}auth")
                if cached:
                    self.auth_id = cached
                    self.headers["X-Auth"] = self.auth_id
                    return True
            except Exception as e:
                logger.debug(f"Redis auth read error: {e}")

        # 2. Логинимся в API
        try:
            url = urljoin(self.url, "auth/login")
            async with ClientSession(timeout=self.timeout) as s:
                async with s.post(url, json={"login": self.login, "password": self.password}, headers=self.headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        self.auth_id = data.get("AuthId")
                        self.headers["X-Auth"] = self.auth_id
                        
                        # Сохраняем в Redis
                        if redis:
                            await redis.set(f"{self.cache_prefix}auth", self.auth_id, ex=1800)
                        return True
                    else:
                        logger.error(f"❌ GLONASS Auth Error: {r.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ GLONASS Connection Error: {e}")
            return False

    async def get_last_data(self, ids: list[int]) -> list[VehicleData]:
        await self._ensure_auth()
        cache_key = f"{self.cache_prefix}{'_'.join(map(str, sorted(ids)))}"
        redis = await self._get_redis()
        
        # Проверяем кэш
        if redis:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    return [VehicleData(**v) for v in json.loads(cached)]
            except Exception as e:
                logger.debug(f"Redis cache read error: {e}")

        # ✅ ИСПРАВЛЕНО: vehiclesIds (plural, camelCase) как в документации
        url = urljoin(self.url, "vehicles/getlastdata")
        payload = {
            "vehiclesIds": ids,  # ← КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ!
            "includeSensors": False  # Не нужны датчики для бота
        }
        
        async with ClientSession(timeout=self.timeout) as session:
            async with session.post(url, json=payload, headers=self.headers) as r:
                if r.status == 200:
                    raw = await r.json()
                    # API возвращает массив объектов
                    vehicles_raw = raw if isinstance(raw, list) else []
                    
                    # Парсим с учётом правильных алиасов
                    vehicles = [VehicleData(**v) for v in vehicles_raw if v.get("latitude") is not None]
                    
                    # Кэшируем
                    if redis and vehicles:
                        try:
                            await redis.set(cache_key, json.dumps([v.model_dump() for v in vehicles]), ex=config.CACHE_TTL_DATA)
                        except Exception as e:
                            logger.debug(f"Redis cache write error: {e}")
                    
                    return vehicles
                else:
                    error_text = await r.text()
                    logger.error(f"❌ GLONASS API Error {r.status}: {error_text[:500]}")
                    return []

    # async def get_last_data(self, ids: list[int]) -> list[VehicleData]:
    #     # Если не смогли авторизоваться — возвращаем пустой список (чтобы бот не упал)
    #     if not await self._ensure_auth():
    #         return []

    #     redis = await self._get_redis()
    #     cache_key = f"{self.cache_prefix}{'_'.join(map(str, sorted(ids)))}"
        
    #     # 1. Проверяем кэш
    #     if redis:
    #         try:
    #             cached = await redis.get(cache_key)
    #             if cached:
    #                 return [VehicleData(**v) for v in json.loads(cached)]
    #         except Exception as e:
    #             logger.debug(f"Redis cache read error: {e}")

    #     # 2. Запрашиваем с API
    #     try:
    #         url = urljoin(self.url, "vehicles/getlastdata")
    #         async with ClientSession(timeout=self.timeout) as s:
    #             async with s.post(url, json=ids, headers=self.headers) as r:
    #                 if r.status == 200:
    #                     raw = await r.json()
    #                     vehicles = [VehicleData(**v) for v in raw if v.get("latitude") is not None]
                        
    #                     # Сохраняем в кэш
    #                     if redis and vehicles:
    #                         try:
    #                             await redis.set(cache_key, json.dumps([v.model_dump() for v in vehicles]), ex=60)
    #                         except Exception as e:
    #                             logger.debug(f"Redis cache write error: {e}")
                        
    #                     return vehicles
    #                 else:
    #                     logger.error(f"❌ GLONASS API Error: {r.status}")
    #                     return []
    #     except Exception as e:
    #         logger.error(f"❌ GLONASS Request Failed: {e}")
    #         return []

    async def close(self):
        if self.redis:
            await self.redis.close()