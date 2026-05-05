### 📝 1. Новый эндпоинт `/api/v1/history` в `alert_api/app/main.py`

Добавь этот код после существующих роутов:

```python
@app.get("/api/v1/history")
async def get_history(
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,
    _: str = Depends(verify_admin_key)
):
    """
    Чтение истории из JSONL-лога.
    - limit: кол-во записей (max 200)
    - offset: сдвиг от конца (0=последние, 50=предыдущие 50)
    - source: фильтр по источнику (опционально)
    """
    log_path = get_active_log_path()
    if not os.path.exists(log_path):
        return []
    
    limit = min(limit, 200)  # Защита от чтения слишком большого куска
    
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # Читаем с конца файла
    total = len(lines)
    start_idx = max(0, total - offset - limit)
    end_idx = max(0, total - offset)
    
    events = []
    for line in lines[start_idx:end_idx]:
        try:
            evt = json.loads(line.strip())
            if source is None or evt.get("source") == source:
                events.append(evt)
        except json.JSONDecodeError:
            continue
    
    return {
        "total_lines": total,
        "returned": len(events),
        "offset": offset,
        "limit": limit,
        "events": list(reversed(events))  # Возвращаем от новых к старым
    }
```

---

### 📚 2. Коллекция curl-запросов

#### 🔐 A. Внешние системы (отправка событий)

**1. SCADA-система (критическое событие):**

```bash
curl -X POST http://192.168.1.100:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk_scada_prod_a8f3k29d..." \
  -d '{
    "location": "Цех Розлива, Линия 3",
    "parameter": "Давление в магистрали",
    "value": 5.8,
    "unit": "МПа",
    "min_val": 2.0,
    "max_val": 5.0,
    "description": "Превышение верхнего порога! Требуется сброс давления",
    "severity": "critical"
  }'
```

**2. 1С:Бухгалтерия (инфо-сообщение):**

```bash
curl -X POST http://192.168.1.100:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk_erp_acc_m2x9p1..." \
  -d '{
    "location": "Сервер 1С",
    "description": "Завершена выгрузка проводок за апрель 2026. Обработано 1542 документа",
    "severity": "info"
  }'
```

**3. Система видеонаблюдения (предупреждение):**

```bash
curl -X POST http://192.168.1.100:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk_cctv_sec_k9x2m..." \
  -d '{
    "location": "КПП №2, Камера 5",
    "description": "Обнаружено движение в нерабочее время (23:45)",
    "severity": "warning"
  }'
```

**4. Ошибка авторизации (нет токена):**

```bash
curl -X POST http://192.168.1.100:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{"description":"Это будет отвергнуто"}'
# Ответ: 401 Unauthorized
```

**5. Ошибка авторизации (невалидный токен):**

```bash
curl -X POST http://192.168.1.100:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer invalid_token" \
  -d '{"description":"Тоже отвергнуто"}'
# Ответ: 403 Forbidden
```

---

#### 🤖 B. Alert Bot (получение маршрутов)

**1. Получить все маршруты (source → room):**

```bash
curl -X GET "http://192.168.1.100:8000/api/v1/config/routes" \
  -H "X-Bot-Token: your_secure_bot_token_here"
```

**Ответ:**

```json
{
  "scada_critical": "!tech_alerts:matrix.vpk-oil.ru",
  "erp_accounting": "!buh_chat:matrix.vpk-oil.ru",
  "cctv_security": "!security_room:matrix.vpk-oil.ru"
}
```

---

#### 👑 C. Администратор (управление и мониторинг)

**1. Получить список всех источников:**

```bash
curl -X GET "http://192.168.1.100:8000/api/v1/admin/sources" \
  -H "admin-key: your_strong_admin_key_here"
```

**2. Добавить новый источник:**

```bash
curl -X POST "http://192.168.1.100:8000/api/v1/admin/sources" \
  -H "admin-key: your_strong_admin_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "fire_alarm",
    "token": "sk_fire_x9k2m...",
    "room": "!security_room:matrix.vpk-oil.ru",
    "comment": "Пожарная сигнализация, корпус А"
  }'
```

**3. Прочитать историю событий (последние 20):**

```bash
curl -X GET "http://192.168.1.100:8000/api/v1/history?limit=20&offset=0" \
  -H "admin-key: your_strong_admin_key_here"
```

**4. Прочитать историю (следующие 50, пропустив последние 100):**

```bash
curl -X GET "http://192.168.1.100:8000/api/v1/history?limit=50&offset=100" \
  -H "admin-key: your_strong_admin_key_here"
```

**5. История только для конкретного источника:**

```bash
curl -X GET "http://192.168.1.100:8000/api/v1/history?limit=50&offset=0&source=scada_critical" \
  -H "admin-key: your_strong_admin_key_here"
```

**Ответ:**

```json
{
  "total_lines": 1523,
  "returned": 50,
  "offset": 0,
  "limit": 50,
  "events": [
    {
      "source": "scada_critical",
      "location": "Цех Розлива",
      "parameter": "Давление",
      "value": 5.8,
      "severity": "critical",
      "timestamp": "2026-05-04T16:45:23.123456"
    },
    ...
  ]
}
```

**6. Health check:**

```bash
curl -X GET "http://192.168.1.100:8000/health"
# Ответ: {"status":"ok","redis":true}
```

**7. Перезагрузить токены (после правки sources.json вручную):**

```bash
curl -X POST "http://192.168.1.100:8000/admin/reload-tokens" \
  -H "admin-key: your_strong_admin_key_here"
```

---


## Интеграция с Alert Bus

### Базовый URL
`http://alert-api:8000/api/v1/ingest`

### Аутентификация
Заголовок: `Authorization: Bearer <ваш_токен>`

### Минимальный запрос
```bash
curl -X POST http://alert-api:8000/api/v1/ingest \
  -H "Authorization: Bearer <токен>" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Текст события"
  }'
```

### Полный запрос

```json
{
  "location": "Где произошло",
  "description": "Что случилось",
  "parameter": "Имя параметра",
  "value": 123.45,
  "unit": "Ед. измерения",
  "min_val": 0,
  "max_val": 100,
  "severity": "info|warning|critical"
}
```

### Правила

- ✅ `severity`: `info` (инфо), `warning` (предупреждение), `critical` (критично)
- ✅ `value`, `min_val`, `max_val`: только числа (float)
- ✅ Все поля кроме `description` — опциональны
- ❌ НЕ передавай поле `source` — оно определяется токеном автоматически

### Примеры кода

**Python:**

```python
import requests

requests.post(
    "http://alert-api:8000/api/v1/ingest",
    headers={
        "Authorization": "Bearer sk_your_token",
        "Content-Type": "application/json"
    },
    json={
        "location": "Цех 1",
        "description": "Температура превышена",
        "severity": "warning"
    }
)
```

**1C (HTTP-соединение):**

```bsl
Заголовок = Новый HTTPЗаголовок();
Заголовок.Добавить("Authorization", "Bearer sk_your_token");
Заголовок.Добавить("Content-Type", "application/json");

Тело = "{""location"":""Цех 1"",""description"":""Температура превышена"",""severity"":""warning""}";

HTTP = Новый HTTPСоединение("alert-api", 8000);
Ответ = HTTP.ОтправитьДляОбработки("POST", "/api/v1/ingest", Тело, Заголовок);
```
