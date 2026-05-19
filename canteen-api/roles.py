# roles.py
import os, json, threading

USERS_FILE = "/app/config/canteen_users.json"
_cache = {}
_lock = threading.Lock()

def load_users():
    global _cache
    try:
        if not os.path.exists(USERS_FILE):
            os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
        print(f"✅ Loaded {len(_cache)} canteen users")
    except Exception as e:
        print(f"⚠️ Error loading roles: {e}")
        _cache = {}

def get_roles(mxid: str) -> list:
    return _cache.get(mxid, ["client"])

def has_role(mxid: str, required_roles: list) -> bool:
    return bool(set(get_roles(mxid)) & set(required_roles))

# Загружаем при импорте модуля
load_users()