import asyncio
import aiohttp
import os
import json
import sys
from dotenv import load_dotenv
import logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from service.glonass_api import GlonassClient

logger = logging.getLogger(__name__)

async def main():
    print("🔍 Диагностика GLONASS API — исправленный формат")
    print(f"📡 URL: {config.GLONASS_URL}")
    print(f"🚌 BUS_IDS: {config.BUS_IDS[:3]}...")
    print("-" * 50)

    client = GlonassClient()
    try:
        # 1. Авторизация
        print("1️⃣ Авторизация...")
        await client._ensure_auth()
        if not client.auth_id:
            print("❌ Не удалось получить AuthId")
            return
        print(f"✅ AuthId: {client.auth_id[:20]}...")
        logger.info(f"🔑 GLONASS AuthId: {client.auth_id}")
        print(f"✅ AuthId получен: {client.auth_id}")  # Дублируем в stdout для docker exec
        print(f"📋 CURL пример:")
        print(f"curl -X POST {config.GLONASS_URL.rstrip('/')}/vehicles/getlastdata \\")
        print(f"  -H 'Content-Type: application/json' \\")
        print(f"  -H 'X-Auth: {client.auth_id}' \\")
        print(f"  -d '{{\"vehicleIds\": [413422, 413424]}}'")

        # 2. Тест getlastdata с ИСПРАВЛЕННЫМ форматом
        print("\n2️⃣ Запрос координат (vehicles/getlastdata)...")
        test_ids = config.BUS_IDS[:3]
        print(f"🎯 Тестируем ID: {test_ids}")
        
        # Прямой запрос для отладки
        
        url = f"{config.GLONASS_URL.rstrip('/')}/vehicles/getlastdata"
        payload = {"vehicleIds": test_ids}  # ✅ Правильный формат
        headers = client.headers.copy()
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                raw = await resp.text()
                print(f"📡 Статус: {resp.status}")
                
                if resp.status == 200:
                    data = json.loads(raw)
                    vehicles = data if isinstance(data, list) else data.get("data", [])
                    print(f"✅ Получено записей: {len(vehicles)}")
                    
                    for v in vehicles:
                        vid = v.get("vehicleId")
                        vnum = v.get("vehicleNumber") or "N/A"  # Handle None
                        lat = v.get("latitude")
                        lng = v.get("longitude")
                        addr = v.get("address") or "N/A"
                        status = "📍 OK" if lat is not None else "⚪ NO_COORDS"
                        print(f"  {status} | ID={vid} | №{vnum} | {lat}, {lng} | 🏠 {addr}")
                else:
                    print(f"❌ Ошибка: {resp.status}")
                    print(f"📄 Ответ: {raw[:800]}")

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())