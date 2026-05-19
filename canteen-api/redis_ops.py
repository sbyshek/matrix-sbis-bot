import redis, json, os
from typing import Dict
from datetime import datetime, timezone

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)

MENU_LIFE_TIME = 86400 * 4
CART_LIFE_TIME = 7200
SHARED_CART_UUID = "canteen_shared_global"

def get_menu() -> dict | None:
    raw = r.get("today_menu")
    return json.loads(raw) if raw else None

def set_menu(menu_dict: dict):
    menu_dict["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    r.setex("today_menu", MENU_LIFE_TIME, json.dumps(menu_dict, ensure_ascii=False))
    remove_all_carts()  # Новое меню = чистые корзины

def get_raw_cart(mxid: str) -> Dict[str, int]:
    cart = r.hgetall(f"raw_cart:{mxid}")
    return {k: int(v) for k, v in cart.items()}

def add_to_raw_cart(mxid: str, dish_uuid: str, delta: int = 1) -> Dict[str, int]:
    key = f"raw_cart:{mxid}"
    r.hincrby(key, dish_uuid, delta)
    r.expire(key, CART_LIFE_TIME)
    return get_raw_cart(mxid)

def remove_from_raw_cart(mxid: str, dish_uuid: str) -> Dict[str, int]:
    key = f"raw_cart:{mxid}"
    r.hdel(key, dish_uuid)
    return get_raw_cart(mxid)

def remove_raw_cart(mxid: str):
    r.delete(f"raw_cart:{mxid}")

def confirm_raw_cart(mxid: str):
    raw = r.hgetall(f"raw_cart:{mxid}")
    if not raw: return
    # Переносим в confirmed
    conf_key = f"confirmed_cart:{mxid}"
    r.delete(conf_key)
    for uuid_val, qty in raw.items():
        r.hset(conf_key, uuid_val, int(qty))
    r.expire(conf_key, CART_LIFE_TIME * 6)
    # Пушим в shared
    for uuid_val, qty in raw.items():
        r.hincrby(f"shared_cart:{SHARED_CART_UUID}", uuid_val, int(qty))
    # Чистим черновик
    r.delete(f"raw_cart:{mxid}")

def get_all_confirmed_carts() -> Dict[str, Dict[str, int]]:
    all_carts = {}
    pattern = "confirmed_cart:*"
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor, match=pattern, count=100)
        for key in keys:
            mxid = key.split(":")[1]
            cart = r.hgetall(key)
            all_carts[mxid] = {k: int(v) for k, v in cart.items()}
        if cursor == 0: break
    return all_carts

def get_shared_cart() -> Dict[str, int]:
    raw = r.hgetall(f"shared_cart:{SHARED_CART_UUID}")
    return {k: int(v) for k, v in raw.items()}

def clear_shared_cart():
    r.delete(f"shared_cart:{SHARED_CART_UUID}")

def remove_all_carts():
    try:
        for pattern in ["raw_cart:*", "confirmed_cart:*", f"shared_cart:{SHARED_CART_UUID}"]:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=pattern, count=100)
                if keys: r.delete(*keys)
                if cursor == 0: break
    except Exception as e:
        print(f"⚠️ Error clearing carts: {e}")
        
# ... в конце файла redis_ops.py ...

def cancel_confirmed_cart(mxid: str):
    """
    Отменяет подтвержденный заказ VIP-клиента:
    1. Вычитает блюда из shared_cart (общий заказ для кухни)
    2. Удаляет confirmed_cart клиента
    3. Очищает raw_cart клиента (чтобы начать с нуля)
    """
    confirmed_key = f"confirmed_cart:{mxid}"
    
    # 1. Получаем список блюд в подтвержденном заказе
    items = r.hgetall(confirmed_key)
    if not items:
        return False  # У клиента нет подтвержденного заказа

    # 2. Вычитаем эти блюда из общего заказа кухни (hincrby поддерживает отрицательные значения)
    shared_key = f"shared_cart:{SHARED_CART_UUID}"
    for dish_uuid, qty in items.items():
        new_val = r.hincrby(shared_key, dish_uuid, -int(qty))
        # 🔥 Если стало 0 или минус → удаляем ключ из хеша
        if new_val <= 0:
            r.hdel(shared_key, dish_uuid)
        

    # 3. Удаляем подтвержденную корзину и черновик клиента
    r.delete(confirmed_key)
    r.delete(f"raw_cart:{mxid}")
    
    return True