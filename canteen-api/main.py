import os, json, uuid, asyncio
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, Header, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime

import parser as menu_parser  # Твой файл парсера
import redis_ops, roles

import logging

# Настройка логгера
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("canteen.token")


# WIDGET_SECRET = os.getenv("CANTEEN_WIDGET_SECRET", "x1RBFjF59qgsfC2ehHB9ou0N9NJxQbuZjEE2UVRa6cPTdKL19mRek2Glt1auqeX2")
WIDGET_SECRET = os.getenv("CANTEEN_WIDGET_SECRET" )

app = FastAPI(title="Canteen API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vpk-oil.ru"],
    allow_methods=["*"],
    allow_headers=["*"]
)


async def verify_widget_token(
    request: Request,
    x_widget_secret: Optional[str] = Header(None, alias="X-Widget-Secret"),
    widget_token: Optional[str] = Query(None)
):
    """
    Принимает токен из заголовка (AJAX) или из URL-параметра (window.open / печать).
    """
    # Приоритет: заголовок → параметр URL
    token = x_widget_secret or widget_token
    
    logger.info(f"🔍 Token check: {request.url.path} | Source: {'Header' if x_widget_secret else 'QueryParam'}")
    
    received = token.strip() if token else ""
    expected = WIDGET_SECRET.strip() if WIDGET_SECRET else ""
    
    if received != expected:
        logger.warning(f"️ 403: Token mismatch! Got len={len(received)}, Expected len={len(expected)}")
        raise HTTPException(status_code=403, detail="Access denied. Invalid widget token.")
    
    logger.info("✅ Token verified")
    return True

# 🔑 Dependency: извлекаем matrix_user_id из query или headers
def get_mxid(request: Request) -> str:
    return (
        request.query_params.get("$matrix_user_id") or
        request.query_params.get("matrix_user_id") or
        request.headers.get("X-Matrix-User-Id") or
        "unknown"
    )

# 🔐 Dependency: проверка ролей
def require_roles(required: List[str]):
    def checker(mxid: str = Depends(get_mxid)):
        user_roles = roles.get_roles(mxid)
        if not set(required) & set(user_roles):
            raise HTTPException(403, f"Access denied. Required: {', '.join(required)}")
        return mxid
    return checker

# 📦 Request Models
class CartUpdate(BaseModel):
    dish_uuid: str
    qty: int = 1  # >0 добавляет, <0 удаляет

class UserUpdate(BaseModel):
    mxid: str
    roles: List[str]

# ==========================================
# 🍽 ЭНДПОИНТЫ МЕНЮ И КАРТОЧКИ
# ==========================================

@app.get("/api/me/roles")
def api_roles(
    mxid: str = Depends(get_mxid),
    _: bool = Depends(verify_widget_token)
    ):
    r = roles.get_roles(mxid)
    if not r: raise HTTPException(403, "User not found in canteen_users.json")
    return {"mxid": mxid, "roles": r}

@app.get("/api/menu")
def api_menu(_: bool = Depends(verify_widget_token)):
    m = redis_ops.get_menu()
    if not m: raise HTTPException(404, "Menu not uploaded. Ask kitchen staff to upload .docx")
    return m

@app.post("/api/admin/upload_menu")
async def api_upload_menu(
    file: UploadFile = File(...),
    mxid: str = Depends(require_roles(["kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
):
    if not file.filename.endswith(".docx"):
        raise HTTPException(400, "Only .docx files allowed")
    
    tmp = f"/tmp/{uuid.uuid4().hex}.docx"
    with open(tmp, "wb") as f: f.write(await file.read())
    
    # Парсим в отдельном потоке, чтобы не блокировать FastAPI
    loop = asyncio.get_event_loop()
    parsed = await loop.run_in_executor(None, menu_parser.parse_docx, tmp)
    os.remove(tmp)
    
    redis_ops.set_menu(parsed.model_dump())
    return {"status": "ok", "items": len(parsed.breakfast.dishes) + len(parsed.lunch.dishes)}

# ==========================================
# 🛒 ЭНДПОИНТЫ КОРЗИНЫ (Клиент)
# ==========================================

@app.get("/api/cart")
def api_cart(
    mxid: str = Depends(require_roles(["client", "vip", "kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    return redis_ops.get_raw_cart(mxid)

@app.post("/api/cart")
def api_cart_update(
    item: CartUpdate,
    mxid: str = Depends(require_roles(["client", "vip", "kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
):
    if item.qty > 0:
        return redis_ops.add_to_raw_cart(mxid, item.dish_uuid, item.qty)
    else:
        return redis_ops.remove_from_raw_cart(mxid, item.dish_uuid)

@app.delete("/api/cart")
def api_cart_clear(
    mxid: str = Depends(require_roles(["client", "vip", "kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    redis_ops.remove_raw_cart(mxid)
    return {"status": "cleared"}

@app.post("/api/cart/confirm")
def api_cart_confirm(
    mxid: str = Depends(require_roles(["vip", "kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    cart = redis_ops.get_raw_cart(mxid)
    if not cart:
        raise HTTPException(400, "Cart is empty")
    redis_ops.confirm_raw_cart(mxid)
    return {"status": "confirmed", "items_count": len(cart)}

# ==========================================
# 👨‍🍳 ЭНДПОИНТЫ КУХНИ
# ==========================================


@app.get("/api/kitchen/orders")
def api_kitchen_orders(
    mxid: str = Depends(require_roles(["kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    """Заказы в разрезе по пользователям (аналог all_confirmed_cart)"""
    return redis_ops.get_all_confirmed_carts()

@app.get("/api/kitchen/summary")
def api_kitchen_summary(mxid: str = Depends(require_roles(["kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    """Суммарный заказ по блюдам для готовки (аналог shared_cart)"""
    return redis_ops.get_shared_cart()

@app.delete("/api/kitchen/summary")
def api_clear_kitchen_summary(mxid: str = Depends(require_roles(["kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    """Очистить общий заказ после выдачи"""
    redis_ops.clear_shared_cart()
    return {"status": "cleared"}

# ==========================================
# ⚙️ АДМИНКА РОЛЕЙ
# ==========================================

@app.get("/api/admin/users")
def api_users(mxid: str = Depends(require_roles(["admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    return roles._cache

@app.post("/api/admin/users")
def api_add_user(
    u: UserUpdate, 
    mxid: str = Depends(require_roles(["admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    roles._cache[u.mxid] = u.roles
    with open(roles.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(roles._cache, f, indent=2, ensure_ascii=False)
    return {"status": "added", "mxid": u.mxid, "roles": u.roles}

@app.delete("/api/admin/users/{target_mxid}")
def api_del_user(
    target_mxid: str,
    mxid: str = Depends(require_roles(["admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    roles._cache.pop(target_mxid, None)
    with open(roles.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(roles._cache, f, indent=2, ensure_ascii=False)
    return {"status": "removed", "mxid": target_mxid}

# ... после эндпоинтов корзины ...

@app.post("/api/cart/cancel")
def api_cancel_order(mxid: str = Depends(require_roles(["vip", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    """Отмена подтвержденного заказа (только для VIP)"""
    success = redis_ops.cancel_confirmed_cart(mxid)
    if not success:
        raise HTTPException(status_code=400, detail="Нет активного подтвержденного заказа")
    return {"status": "cancelled"}



@app.get("/api/kitchen/summary/print", response_class=HTMLResponse)
def print_summary(mxid: str = Depends(require_roles(["kitchen", "admin"])),
    _: bool = Depends(verify_widget_token)
    ):
    summary = redis_ops.get_shared_cart()
    if not summary:
        return HTMLResponse(content="<h1>Заказов нет</h1>")
    
    # 🔥 Фильтруем: оставляем только qty > 0
    valid_items = {uid: int(qty) for uid, qty in summary.items() if int(qty) > 0}
    if not valid_items:
        return HTMLResponse(content="<h1>Нет активных блюд для печати</h1>")
        
    # Маппинг UUID → Название (берём из текущего меню)
    menu = redis_ops.get_menu()
    all_dishes = {}
    if menu:
        for d in (menu.get('breakfast', {}).get('dishes', []) + menu.get('lunch', {}).get('dishes', [])):
            all_dishes[d['uuid']] = d['name']

    # Группировка по категориям для печати
    grouped = {}
    for uid, qty in valid_items.items():
        # cat = all_dishes.get(uid, 'Прочее')
        name = all_dishes.get(uid, uid)
        cat = next((c for c in ['Салаты','Первые блюда','Вторые блюда','Гарниры','Напитки','Хлеб','Соусы','Десерты'] if c.lower() in name.lower()), '📦 Общее')
        grouped.setdefault(cat, []).append({'name': name, 'qty': int(qty)})

    rows = ""
    for cat, items in grouped.items():
        rows += f"<h3>{cat}</h3><table><tr><th>Блюдо</th><th>Кол-во</th></tr>"
        for item in items:
            rows += f"<tr><td>{item['name']}</td><td style='text-align:center; font-weight:bold;'>{item['qty']}</td></tr>"
        rows += "</table><br>"

        html = f"""<!DOCTYPE html><html><head><title>Сводный заказ</title>
        <style>
        body {{ font-family: sans-serif; padding: 20px; }}
        h1 {{ text-align: center; margin-bottom: 5px; }}
        .date {{ text-align: center; color: #666; margin-bottom: 30px; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
        th, td {{ border: 2px solid #333; padding: 10px; font-size: 18px; }}
        th {{ background: #f0f0f0; text-align: left; }}
        td:last-child {{ text-align: center; width: 100px; }}
        @media print {{ .no-print {{ display: none; }} body {{ padding: 0; }} }}
        </style></head><body>
        <h1>🍽️ Сводный заказ на кухню</h1>
        <div class="date">{datetime.now().strftime('%d.%m.%Y %H:%M')}</div>
        {rows}
        <button class="no-print" onclick="window.print()" style="padding:15px 30px; font-size:20px; cursor:pointer; background:#3b82f6; color:#fff; border:none; border-radius:8px; display:block; margin: 30px auto;">🖨️ Распечатать</button>
        </body></html>"""
    return HTMLResponse(content=html)

@app.get("/api/cart/confirmed")
def api_cart_confirmed(mxid: str = Depends(get_mxid),
    _: bool = Depends(verify_widget_token)
    ):
    """Проверяет, есть ли у пользователя активный подтвержденный заказ"""
    confirmed = redis_ops.r.hgetall(f"confirmed_cart:{mxid}")
    if confirmed:
        return {"has_confirmed": True, "items": confirmed}
    return {"has_confirmed": False, "items": {}}