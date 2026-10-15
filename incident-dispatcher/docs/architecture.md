# Архитектура системы обзвона

Документ описывает архитектуру голосового обзвона, ключевые решения и грабли, на которые наступали при разработке.

**Дата последнего обновления:** 2026-10-09  
**Автор:** incident-dispatcher team

---

## Обзор архитектуры

Система обзвона построена по принципу **pull-model**: Asterisk активно запрашивает работу у voice-api, а не получает её push-ом. Это даёт Asterisk контроль над транками и предотвращает перегрузку.

### Схема компонентов

```pre
─────────────────────┐
│ Dashboard (UI)     │
│ React + FastAPI    │
└──────────┬─────────┘
│ POST /alert/trigger-*
▼
┌─────────────────────────────┐
│ incident-dispatcher         │
│ - Сохраняет мету в Redis    │
│ - Внутренние → сразу        │
│ - Внешние → в очередь       │
│ - Отправляет в Matrix       │
└──────────┬──────────────────┘
│
▼
┌─────────────────────────────┐
│ voice-api                   │
│ - Воркер очереди            │
│ - originate_call()          │
│ - POST /campaign/finish     │
│ - trunk_reconcile_loop()    │
└──────────┬──────────────────┘
│ AMI Originate
▼
┌─────────────────────────────┐
│ Asterisk                    │
│ - Диалплан alert_*          │
│ - Dial(SIP/rt_xxx/...)      │
│ - curl → voice-api          │
└──────────┬──────────────────┘
│ SIP INVITE
▼
┌─────────────────────────────┐
│ Провайдер SIP-транков       │
│ (5 транков × 3 канала)      │
└─────────────────────────────┘
```

## Компоненты системы

### incident-dispatcher (FastAPI)

**Роль:** Оркестратор оповещений, UI-бэкенд, интеграция с Matrix.

**Ключевые эндпоинты:**

- `POST /api/v1/alert/trigger-template` — запуск по шаблону
- `POST /api/v1/alert/trigger-custom` — своё сообщение + опционально голос в Matrix
- `POST /api/v1/audio/upload` — загрузка + конвертация в WAV 8kHz mono

**Хранение:**

- `config/locations.json` — локации, комнаты Matrix, подписчики
- `config/templates.json` — шаблоны оповещений
- `data/audio/*.wav` — аудиофайлы (конвертированные)

### voice-api (FastAPI)

**Роль:** Управление Asterisk через AMI, очередь внешних звонков, ретраи.

**Ключевые эндпоинты:**

- `POST /api/v1/campaign/trigger-template` — принимает запрос от диспетчера
- `GET /api/v1/campaign/next` — Asterisk запрашивает следующий номер (pull-model)
- `POST /api/v1/campaign/finish` — Asterisk отчитывается о завершении звонка

**Хранение (Redis БД 3):**

- `voice:campaign:{id}` — метаданные кампании (TTL 2ч)
- `voice:ext_queue` — sorted set: `{campaign}|{number}|{trunk}` → score=timestamp
- `voice:retry:{campaign}:{number}` — счётчик попыток (TTL 1ч)
- `voice:active_trunk:{trunk}` — текущая загрузка транка
- `voice:dedup:{campaign}:{number}` — защита от двойного звонка (TTL 10мин)
- `data/calls.log.jsonl` — история всех звонков (JSON Lines)

### Asterisk

**Роль:** SIP-сервер, инициирует звонки, воспроизводит аудио.

**Диалплан:**

- `alert_anchor` — точка входа (вызывается через AMI Originate)
- `alert_handler` — обработка ответа абонента (воспроизведение, curl-отчёты)
- `alert_hangup` — обработка отбоя (curl-отчёт о прерывании)

**Конфигурация:**

- `/etc/asterisk/extensions_custom.conf` — пользовательские контексты
- Транки: `SIP/rt_3049616`, `SIP/rt_3049625`, `SIP/rt_3049665`, `SIP/rt_3048603`, `SIP/rt_3048554`

---

## Поток данных

### Успешный звонок (answered → completed)

1. Dashboard → incident-dispatcher: POST /alert/trigger-custom
2. incident-dispatcher:
    - Сохраняет мету в Redis
    - Кладёт внешние номера в voice:ext_queue
    - Отправляет текст + аудио в Matrix
3. voice-api (воркер):
    - Берёт номер из очереди
    - Проверяет свободные слоты на транках
    - AMI Originate → Asterisk
4. Asterisk:
    - Dial(SIP/rt_xxx/NUMBER)
    - Абонент ответил → Gosub(alert_handler)
    - curl → voice-api/finish (status=answered)
    - Playback(audio) × 2
    - curl → voice-api/finish (status=completed)
    - Hangup
5. voice-api/finish:
    - Логирует в JSONL
    - Декремент voice:active_trunk:{trunk}

### Отказ провайдера (congestion) + ретрай

1. voice-api (воркер):
    - AMI Originate → Asterisk
2. Asterisk:
    - Dial(SIP/rt_xxx/NUMBER)
    - Провайдер вернул CONGESTION (транк забит)
    - Hangup без alert_handler
    - curl → voice-api/finish (status=unavailable)
3. voice-api/finish:
    - Логирует в JSONL
    - Увеличивает voice:retry:{campaign}:{number}
    - Кладёт в voice:ext_queue с score = now + 30с
    - Декремент voice:active_trunk:{trunk}
4. Через 30 секунд воркер снова берёт этот номер из очереди
5. Если снова unavailable → повтор через 60с, потом 120с, 180с
6. После 4 попыток → абонент помечен недоступным

### Залп внешних звонков бьётся в физику провайдера

**Проблема:**
    Кампания на 51 абонента → 51 originate разом → провайдер принимает ~15 (ёмкость транков), остальные CONGESTION → "unavailable" залпом.

**Решение:**
    Внешние звонки идут не залпом, а через очередь с пейсингом:

```python
# В originate_campaign:
if ch_type == "trunk":
    await redis_client.zadd("voice:ext_queue", {member: time.time()})
else:
    asyncio.create_task(originate_call(...))  # внутренние сразу
```

Воркер выпускает звонки со скоростью освобождения слотов (строб 0.3с между originate).

#### Чек-лист при отладке

**Звонок не пришёл**

1. Проверить JSONL:

    ```bash
    tail -f data/calls.log.jsonl | grep "89131234567"
    ```

    **Нет записи** → звонок не стартовал (проблема в диспетчере или voice-api)
    **Есть** status=unavailable → провайдер отклонил (проблема с транками или ретраями)

2. Проверить очередь

    ```bash
    docker exec matrix-redis redis-cli -n 3 ZCARD voice:ext_queue
    docker exec matrix-redis redis-cli -n 3 ZRANGE voice:ext_queue 0 -1 WITHSCORES
    ```

    - Очередь не пустая → воркер не работает
    - Очередь пустая, но звонок не пришёл → проблема в originate или Asterisk

3. Проверить ретраи:

    ```bash
    docker logs voice-api | grep -E "🔁|♻️"
    ```
    
    - Нет логов → ретрай-логика не срабатывает (проверить redis_client vs redis)
    - Есть логи, но звонок не пришёл → ретрай упирается в dedup или транки

4. Проверить Asterisk:
    
    ```bash
    asterisk -rx "core show channels concise" | grep -c "SIP/rt_"
    ```
    
    - Больше 15 → транки забиты, новые звонки не пройдут
    - 0 каналов, но звонок не пришёл → проблема в диалплане

