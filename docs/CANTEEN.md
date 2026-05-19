# 🍽️ Canteen Widget для Matrix

Корпоративный виджет для **Element/Matrix**, позволяющий сотрудникам столовой управлять меню, а работникам — просматривать блюда, собирать заказы и отслеживать их статус. Работает через REST API + Redis, поддерживает загрузку меню из `.docx`, мобильные устройства и защищён двухуровневой аутентификацией.

---

## ✨ Возможности

| Компонент | Функционал |
|-----------|------------|
|  **Меню** | Автопарсинг из `.docx` (формат VPK Oil), разделение на Завтрак/Обед, группировка по категориям |
| 🛒 **Корзина** | Добавление/удаление блюд, подсчёт суммы, очистка черновика |
| ✅ **Подтверждение** | Отправка заказа на кухню (доступно ролям `vip`, `kitchen`, `admin`) |
| 🚫 **Отмена** | Отзыв подтверждённого заказа с пересчётом сводки кухни |
| 👨‍ **Панель кухни** | Очередь заказов, переключение "По клиентам / По блюдам", смена статусов (`done`/`cancelled`) |
| 🖨️ **Печать** | Адаптивный HTML-отчёт для принтера с крупным шрифтом |
| 📱 **Мобильность** | Кастомные модалы и тосты вместо `alert()`/`confirm()` (полная совместимость с Element iOS/Android) |
| 🔒 **Безопасность** | Токен виджета (`X-Widget-Secret`) + проверка `$matrix_user_id` + ролевая модель |

---

## 🏗️ Архитектура и стек

```
[Matrix Element] → iframe → canteen-widget.html
                      ↓ (AJAX)
[Nginx Proxy] → /canteen-api/ → proxy_pass → [FastAPI Backend]
                      ↓
                 [Redis] (меню, корзины, очередь заказов)
```

- **Backend:** Python 3.11, FastAPI, Uvicorn
- **Cache/DB:** Redis
- **Frontend:** Vanilla JS, HTML5, CSS3 (встроено в виджет)
- **Parser:** `python-docx` (адаптирован под таблицы формата столовой)
- **Infra:** Docker, Docker Compose, Nginx

---

## 📁 Структура проекта

```
canteen-api/
├── main.py              # Роуты FastAPI, middleware, валидация
├── models.py            # Pydantic-схемы (Dish, Menu, Cart, Order)
├── parser.py            # Парсинг .docx → FullMenu
├── redis_ops.py         # Операции с Redis (корзины, очередь, сводки)
├── roles.py             # Управление ролями (canteen_users.json)
├── canteen-widget.html  # Фронтенд виджета (UI + JS-логика)
├── config/
│   └── canteen_users.json # Сопоставление MXID → роли
├── .env                 # Переменные окружения
├── docker-compose.yml   # Оркестрация сервисов
├── Dockerfile           # Образ бэкенда
└── requirements.txt     # Python-зависимости
```

---

## 🚀 Быстрый старт

### 1. Клонирование и подготовка

```bash
git clone https://github.com/<your-org>/canteen-matrix-widget.git
cd canteen-matrix-widget
cp .env.example .env
# Отредактируйте .env (см. раздел ⚙️)
```

### 2. Запуск через Docker Compose

```bash
docker compose up -d --build
docker compose logs -f canteen-api  # Проверка запуска
```

### 3. Настройка Nginx (Web-хост)

```nginx
location = /canteen-widget.html {
    alias /var/www/html/canteen-widget.html;
    add_header Cache-Control "no-cache, no-store, must-revalidate";
}

location /canteen-api/ {
    client_max_body_size 5M;
    rewrite ^/canteen-api/(.*)$ /api/$1 break;
    proxy_pass http://<MATRIX_HOST_IP>:8766/;
    
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Widget-Secret $http_x_widget_secret;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Добавление виджета в Matrix
В комнате столовой выполните:

```
/addwidget https://vpk-oil.ru/canteen-widget.html?matrix_user_id=$matrix_user_id "Столовая" 60 40 80 80
```

---

## ⚙️ Конфигурация

### `.env`

```ini
REDIS_URL=redis://redis:6379/0
WIDGET_SECRET=<ваш_секретный_токен_64_символа>
PORT=8000
```

### `config/canteen_users.json`

```json
{
  "@a.hohlov:matrix.vpk-oil.ru": ["admin", "vip", "kitchen"],
  "@cook1:matrix.vpk-oil.ru": ["kitchen"],
  "@ivanov:matrix.vpk-oil.ru": ["client"]
}

```
> 💡 Пользователи, отсутствующие в файле, по умолчанию получают роль `client` (просмотр + сбор корзины).

---

## 🔐 Безопасность

1. **Токен виджета (`X-Widget-Secret`)**  
   Каждый запрос к API проверяется на совпадение с `WIDGET_SECRET`. Заголовок пробрасывается через Nginx: `proxy_set_header X-Widget-Secret $http_x_widget_secret;`.
2. **Идентификация пользователя**  
   Matrix автоматически подставляет `$matrix_user_id` в URL виджета. Бэкенд валидирует MXID по `canteen_users.json`.
3. **Ролевой контроль**  
   Все критические эндпоинты (`/admin/upload_menu`, `/cart/confirm`, `/kitchen/*`) защищены зависимостью `require_roles([...])`.
4. **Защита от прямых переходов**  
   Виджет работает только внутри iframe Element. Прямой переход по ссылке возвращает `403` (отсутствие токена + реферер).

---

## 👥 Роли и доступы

| Роль | Меню | Корзина | Подтверждение | Панель кухни | Загрузка меню | Управление ролями |
|------|------|---------|---------------|--------------|---------------|------------------|
| `client` | ✅ | ✅ | ❌ |  | ❌ | ❌ |
| `vip` | ✅ | ✅ | ✅ |  | ❌ | ❌ |
| `kitchen` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `admin` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## 📡 API (кратко)

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| `GET` | `/api/me/roles` | Роли текущего пользователя |
| `GET` | `/api/menu` | Текущее меню (breakfast/lunch) |
| `GET` | `/api/cart` | Черновик корзины `{uuid: qty}` |
| `POST` | `/api/cart` | Добавить/убавить блюдо |
| `DELETE` | `/api/cart` | Очистить корзину |
| `POST` | `/api/cart/confirm` | Подтвердить заказ |
| `POST` | `/api/cart/cancel` | Отменить подтверждённый заказ |
| `POST` | `/api/admin/upload_menu` | Загрузить `.docx` меню |
| `GET` | `/api/kitchen/orders` | Очередь заказов по клиентам |
| `GET` | `/api/kitchen/summary` | Сводка по блюдам для готовки |
| `PATCH` | `/api/kitchen/orders/{id}/status` | Сменить статус заказа |

---

## 📱 Мобильная совместимость
  
- Нативные `alert()`/`confirm()` заменены на кастомные компоненты:
- `showToast(message, type)` — всплывающее уведомление
- `showConfirm(message, onConfirm, onCancel)` — модальное окно с кнопками
- Полная поддержка touch-интерфейса, адаптивная вёрстка, отсутствие блокировок iframe на iOS/Android.

---

## ️ Разработка и отладка

```bash
# Горячая перезагрузка кода (volume-маппинг в docker-compose.yml)
volumes:
  - ./main.py:/app/main.py
  - ./parser.py:/app/parser.py
  # ...

# Просмотр логов токена и ролей
docker compose logs -f canteen-api | grep -E "Token|roles|Loaded"

# Тест API через curl
curl -s "https://vpk-oil.ru/canteen-api/menu" \
  -H "X-Widget-Secret: <WIDGET_SECRET>"
```

---

## 📝 Лицензия
Внутренний корпоративный проект. Распространение вне организации запрещено.

---

## 🤝 Поддержка
По вопросам интеграции, правок парсера или расширению ролей обращайтесь к разработчику: `@a.hohlov:matrix.vpk-oil.ru`

