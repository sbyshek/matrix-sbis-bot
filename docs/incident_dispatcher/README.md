# 📡 Система управления инцидентами и оповещениями

Комплексная система для управления инцидентами на промышленных объектах с интеграцией Matrix для коммуникации и Asterisk для голосовых оповещений.

## 🏗️ Архитектура системы

```
┌─────────────────────────────────────────────────────────────┐
│                    Matrix Server                            │
│  (Комнаты для каждой локации + общая комната дашборда)      │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│              incident-dispatcher (порт 8770)                │
│  • Управление инцидентами (внимание/тревога)                │
│  • Единая история событий                                   │
│  • Хранилище аудиофайлов                                    │
│  • Интеграция с Matrix                                      │
│  • Web UI (дашборд)                                         │
│  • Локальные пульты (desk)                                  │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│                 voice-api (порт 8769)                       │
│  • Управление голосовыми кампаниями                         │
│  • Интеграция с Asterisk через AMI                          │
│  • Логирование звонков                                      │
│  • STT (транскрибация голосовых ответов)                    │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│                      Asterisk                               │
│  • Исходящие звонки абонентам                               │
│  • Воспроизведение аудиофайлов                              │
│  • IVR (интерактивное голосовое меню)                       │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Основные возможности

### 📍 Управление инцидентами

- Создание событий "Внимание" и "Тревога" для каждой локации
- Автоматическое закрытие событий по таймауту
- Ручное управление через дашборд или локальные пульты
- Полная история всех событий

### 📢 Голосовые оповещения

- Шаблоны оповещений с аудиофайлами
- Массовое оповещение нескольких локаций
- Тестовые оповещения (запись через браузер)
- Автоматический дозвон с retry-логикой

### 🔔 Matrix интеграция

- Автоматические уведомления в комнаты локаций
- Поддержка форматирования (Markdown)
- История всех уведомлений

### 📞 IVR (интерактивное голосовое меню)

- Запуск оповещений по коду с телефона
- Поддержка дублирующихся кодов с приоритетом по локации
- Интеграция с Asterisk dialplan

### 🎙️ Студия записи

- Запись голосовых сообщений через браузер
- Автоматическая конвертация в формат Asterisk (WAV 8kHz mono)
- Предпрослушивание перед запуском

## 📦 Компоненты системы

| Сервис | Порт | Назначение |
|--------|------|------------|
| `incident-dispatcher` | 8770 | Управление инцидентами, Web UI, аудиофайлы |
| `voice-api` | 8769 | Голосовые кампании, интеграция с Asterisk |
| `redis` | 6379 | Хранение состояния и очередей |
| `asterisk` | 5038 (AMI) | Телефония и голосовые оповещения |

## 🛠️ Установка и настройка

См. **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)** для подробной инструкции по установке и настройке.

## 📖 Быстрый старт

### 1. Клонирование и настройка

```bash
git clone <repository-url>
cd incident-dispatcher-system
cp .env.example .env
# Отредактируйте .env с вашими настройками
```

### 2. Запуск сервисов

```bash
docker compose up -d
```

### 3. Доступ к дашборду

Откройте браузер: `http://your-server:8770`

## 🔐 Безопасность

- Все API защищены токенами (X-API-Token, X-Local-Token, X-Secret)
- IP-фильтрация для дашборда и локальных пультов
- Matrix авторизация для модераторов
- Проверка прав доступа к локациям

## 📊 Мониторинг

```bash
# Логи incident-dispatcher
docker logs -f incident-dispatcher

# Логи voice-api
docker logs -f voice-api

# История событий
curl -H "X-API-Token: $DASHBOARD_TOKEN" \
  http://localhost:8770/api/v1/history?limit=20
```

## 🚧 Roadmap

- [ ] HTTPS через nginx с wildcard сертификатом
- [ ] Расширенная аналитика и отчёты
- [ ] Интеграция с внешними системами мониторинга
- [ ] Мобильное приложение для операторов
- [ ] Поддержка PJSIP вместо chan_sip

## 📝 Лицензия

Proprietary software. Все права защищены.

# 🚨 Incident Dispatcher

Основной сервис управления инцидентами и оповещениями с интеграцией Matrix и голосовыми уведомлениями через Asterisk.

## 📋 Описание

Сервис предоставляет:

- REST API для управления инцидентами (создание, закрытие, история)
- Web UI (дашборд) для операторов
- Локальные пульты для каждой локации
- Хранилище и управление аудиофайлами
- Интеграцию с Matrix для уведомлений
- Интеграцию с voice-api для голосовых оповещений

## 🏗️ Архитектура

```
incident-dispatcher/
├── app/
│   ├── main.py              # FastAPI приложение и маршруты
│   ├── alert_svc.py         # Бизнес-логика оповещений
│   ├── security.py          # Авторизация и проверка доступа
│   └── core/
│       ├── config.py        # Настройки из .env
│       ├── redis.py         # Работа с Redis
│       ├── matrix.py        # Интеграция с Matrix
│       ├── voice.py         # Интеграция с voice-api
│       ├── audio.py         # Управление аудиофайлами
│       ├── history.py       # Единая история событий
│       └── background.py    # Фоновые задачи
├── templates/               # Jinja2 HTML шаблоны
│   ├── dashboard.html       # Главный дашборд
│   ├── location_dashboard.html  # Локальный пульт
│   └── 404.html            # Страница ошибки
├── config/                  # Конфигурационные файлы
│   ├── locations.json      # Локации и их настройки
│   └── templates.json      # Шаблоны оповещений
├── data/
│   └── audio/              # Хранилище аудиофайлов
│       └── custom/         # Пользовательские записи
└── Dockerfile
```

## 🚀 API Endpoints

### Управление инцидентами

```http
POST /api/location/{loc_id}/event
Content-Type: application/json
Authorization: Bearer {matrix_token}

{
  "type": "attention",  // или "alarm"
  "comment": "Обнаружен дым",
  "source": "dispatcher"
}
```

```http
POST /api/location/{loc_id}/close
Content-Type: application/json

{
  "event_id": "abc123"
}
```

```http
POST /api/location/{loc_id}/clear-all
```

### Голосовые оповещения

```http
POST /api/v1/alert/trigger
X-API-Token: {token}
Content-Type: application/json

{
  "template_id": "fire_alarm",
  "loc_id": "warehouse"
}
```

```http
POST /api/v1/alert/trigger-bulk
X-API-Token: {token}
Content-Type: application/json

{
  "template_id": "fire_alarm",
  "loc_ids": ["warehouse", "workshop_1"]
}
```

```http
POST /api/v1/alert/trigger-custom
X-API-Token: {token}
Content-Type: application/json

{
  "audio_file": "custom/test_20260813_113000",
  "loc_ids": ["warehouse"],
  "text_for_matrix": "Тестовое оповещение"
}
```

### Управление аудиофайлами

```http
POST /api/v1/audio/upload
X-API-Token: {token}
Content-Type: multipart/form-data

file: <audio_file>
template_id: "fire_alarm" (опционально)
```

```http
GET /api/v1/audio/list
X-API-Token: {token}
```

```http
GET /api/v1/audio/{filename}
X-Secret: {voice_api_secret}
```

### IVR

```http
POST /api/v1/alert/trigger-by-code
X-Local-Token: {token}
Content-Type: application/json

{
  "code": "9111",
  "caller": "4299"
}
```

### История и статистика

```http
GET /api/v1/history?limit=100&loc_id=warehouse
X-API-Token: {token}
```

```http
GET /api/v1/campaign/{campaign_id}/details
X-API-Token: {token}
```

## 🔐 Аутентификация

### Типы токенов

| Токен | Назначение | Источник |
|-------|-----------|----------|
| `X-API-Token` | Дашборд, операторы | `.env:DASHBOARD_API_TOKEN` |
| `X-Local-Token` | Внутренние сервисы | `.env:LOCAL_API_TOKEN` |
| `X-Secret` | Asterisk → voice-api | `.env:VOICE_API_SECRET` |
| `Authorization: Bearer` | Matrix пользователи | Matrix access token |

### IP-фильтрация

```env
# .env
ALLOWED_IPS=127.0.0.1,172.17.10.10
DASHBOARD_ALLOWED_IPS=192.168.1.0/24,10.0.0.0/8
```

## 📊 Redis структура

```
dispatch:state:{loc_id}              # Текущее состояние локации
dispatch:history:{loc_id}            # История событий локации
dispatch:unified_history             # Глобальная история
dispatch:campaign_launch:{id}        # Детали запуска кампании
```

## 🎨 Web UI

### Главный дашборд (`/`)

- Список всех локаций с текущим статусом
- Создание инцидентов
- Запуск оповещений по шаблонам
- Студия записи тестовых оповещений
- Единая история событий

### Локальный пульт (`/desk/{loc_id}`)

- Доступ только с разрешённых IP
- Управление инцидентами конкретной локации
- Запуск оповещений
- История локации

## 🔧 Конфигурация

### locations.json

```json
{
  "warehouse": {
    "name": "Склад ГСМ",
    "icon": "🛢️",
    "room_id": "!abc123:matrix.example.com",
    "asterisk_node": "zv-asterisk",
    "allowed_ips": ["192.168.1.100", "192.168.1.101"],
    "alerts": [
      {
        "template_id": "fire_alarm",
        "trigger_code": "9111",
        "matrix_text": "🚨 **Склад ГСМ: ПОЖАРНАЯ ТРЕВОГА!**\nИнициатор: {author}\nВремя: {time}",
        "subscribers": ["89234743710:ACX:BCX", "4299:IT:Zavod"]
      }
    ]
  }
}
```

### templates.json

```json
{
  "fire_alarm": {
    "name": "Пожарная тревога",
    "audio_file": "custom/test-message-8000",
    "severity": "alarm",
    "default_text": "🚨 ВНИМАНИЕ! Пожарная тревога. Немедленно покиньте здание."
  }
}
```

## 🐛 Отладка

```bash
# Логи в реальном времени
docker logs -f incident-dispatcher

# Проверка Redis
docker exec -it matrix-redis redis-cli -a "$REDIS_PASSWORD"
> KEYS dispatch:*
> GET dispatch:state:warehouse

# Проверка аудиофайлов
ls -lh incident-dispatcher/data/audio/custom/

# Тест API
curl -H "X-API-Token: $TOKEN" http://localhost:8770/api/v1/history
```

## 📝 Лицензия

Proprietary software. Все права защищены.


# 📞 Voice API

Сервис для управления голосовыми кампаниями через Asterisk AMI с поддержкой очередей, retry-логики и транскрибации голосовых ответов.

## 📋 Описание

Сервис предоставляет:

- REST API для запуска голосовых кампаний
- Интеграцию с Asterisk через AMI (Asterisk Manager Interface)
- Управление очередями звонков с контролем лимитов транков
- Автоматический retry для неуспешных звонков
- Логирование всех звонков в JSONL формат
- STT (Speech-to-Text) для голосовых ответов
- Проксирование IVR-запросов в incident-dispatcher

## 🏗️ Архитектура

```

voice-api/
├── app/
│   ├── main.py                    # FastAPI приложение
│   ├── core/
│   │   ├── config.py             # Настройки и конфигурация Asterisk узлов
│   │   ├── dependencies.py       # FastAPI зависимости
│   │   └── subscribers.py        # Парсинг абонентов
│   └── services/
│       ├── asterisk_manager.py   # AMI клиент и управление звонками
│       └── stt.py                # Whisper STT сервис
├── config/
│   └── asterisk_nodes.json       # Конфигурация Asterisk узлов
├── data/
│   └── voice/                    # Временные аудиофайлы и логи
│       └── calls.log.jsonl       # Лог всех звонков
└── Dockerfile

```

## 🚀 API Endpoints

### Запуск кампаний

```http
POST /api/v1/campaign/trigger-template
X-Secret: {secret}
Content-Type: application/json

{
  "node_id": "zv-asterisk",
  "template_id": "fire_alarm",
  "audio_file": "custom/test-message-8000",
  "text_for_matrix": "Пожарная тревога",
  "subscribers": ["89234743710:ACX:BCX", "4299:IT:Zavod"],
  "loc_id": "warehouse",
  "loc_name": "Склад ГСМ",
  "initiator": "dashboard_user"
}
```

**Ответ:**

```json
{
  "status": "success",
  "campaign_id": "abc123def456",
  "template_id": "fire_alarm",
  "location": "Склад ГСМ",
  "total_subscribers": 2,
  "results": {
    "success": 2,
    "error": 0,
    "details": [...]
  }
}
```

### Управление очередью

```http
GET /api/v1/campaign/next
X-Secret: {secret}
```

**Ответ:** `OK|89234743710|ACX|BCX|dashboard_user` или `WAIT`

```http
POST /api/v1/campaign/finish
X-Secret: {secret}
Content-Type: application/x-www-form-urlencoded

number=89234743710&status=completed&trunk=SIP/rt_3049665&event=call_finished&campaign_id=abc123
```

### IVR проксирование

```http
POST /api/v1/campaign/trigger-ivr
X-Secret: {secret}
Content-Type: application/json

{
  "code": "9111",
  "caller": "4299"
}
```

### Голосовые ответы (STT)

```http
POST /api/v1/feedback
X-Secret: {secret}
Content-Type: multipart/form-data

file: <audio.wav>
caller_id: "89234743710"
initiator: "system"
exten: "8000"
```

### Мониторинг

```http
GET /api/v1/campaigns/list?limit=50
X-Secret: {secret}
```

```http
GET /api/v1/campaign/{campaign_id}/logs
X-Secret: {secret}
```

```http
GET /api/v1/campaign/{campaign_id}/status
X-Secret: {secret}
```

```http
GET /api/v1/debug/queue
X-Secret: {secret}
```

```http
DELETE /api/v1/debug/reset
X-Secret: {secret}
```

## 📞 Asterisk интеграция

### Dialplan контексты

Сервис использует следующие контексты в Asterisk:

- `pa_call_file_new` — исходящий звонок
- `alert_handler` — воспроизведение аудио после ответа
- `alert_anchor` — якорь для первой половины Local канала
- `alert_hangup` — обработчик завершения звонка
- `log-fail` — логирование неуспешных звонков

### Переменные канала

```
TARGET_NUMBER       Номер абонента
AUDIO_FILE          Путь к аудиофайлу
INITIATOR           Инициатор звонка
TARGET_EXTEN        Исходный номер
ALERT_CALLERID      Caller ID
DIAL_STRING         Строка дозвона
CHANNEL_TYPE        Тип канала (trunk/SIP/IAX2)
EXTERNAL_TRUNK      Имя транка
CAMPAIGN_ID         ID кампании
```

### Лимиты транков

Каждый транк имеет лимит одновременных звонков (`max_concurrent_per_trunk`). Сервис отслеживает загрузку через Redis:

```
voice:active_trunk:{trunk_name}
```

При достижении лимита звонки ставятся в очередь.

## 🔄 Retry логика

Для неуспешных звонков (кроме `noanswer` и `busy`) сервис автоматически повторяет попытку:

- Максимум попыток: 2
- Задержка между попытками: 60 секунд
- Retry-очередь: `voice:retry:{campaign_id}`

## 📊 Логирование звонков

Все звонки логируются в `data/voice/calls.log.jsonl`:

```json
{
  "timestamp": "2026-08-13T10:30:00+07:00",
  "event": "call_finished",
  "number": "89234743710",
  "status": "completed",
  "trunk": "SIP/rt_3049665",
  "campaign_id": "abc123def456"
}
```

## 🔧 Конфигурация

### asterisk_nodes.json

```json
{
  "nodes": [
    {
      "id": "zv-asterisk",
      "name": "Астериск ЗАВОД",
      "host": "172.17.10.10",
      "port": 5038,
      "username": "voice_api",
      "secret": "acb39a7e125e7f4ea548bf3af5c949ef",
      "internal_prefix": "4",
      "external_trunks": ["SIP/rt_3049616", "SIP/rt_3049625"],
      "internal_max_length": 4,
      "max_concurrent_per_trunk": 3,
      "caller_id": "Голосовое оповещение <9000>",
      "active": true,
      "routing_rules": [
        {
          "pattern": "^4[0-4]\\d{2}$",
          "channel": "SIP",
          "description": "Локальные SIP-номера"
        },
        {
          "pattern": "^8\\d{10}$",
          "channel": "trunk",
          "description": "Внешние мобильные"
        }
      ]
    }
  ]
}
```

### .env

```env
VOICE_API_SECRET=your-secret-here
REDIS_URL=redis://redis:6379/3
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
INCIDENT_DISPATCHER_URL=http://incident-dispatcher:8000
INCIDENT_DISPATCHER_TOKEN=your-local-token
```

## 🎙️ STT (Speech-to-Text)

Сервис использует `faster-whisper` для транскрибации голосовых ответов:

- Модель: `small` (баланс скорости и качества)
- VAD фильтр для удаления тишины
- Поддержка промышленных терминов через `WHISPER_PROMPT`

## 🐛 Отладка

```bash
# Логи в реальном времени
docker logs -f voice-api

# Проверка очереди
curl -H "X-Secret: $SECRET" http://localhost:8769/api/v1/debug/queue

# Сброс счётчиков (экстренно)
curl -X DELETE -H "X-Secret: $SECRET" http://localhost:8769/api/v1/debug/reset

# Проверка логов звонков
tail -f voice-api/data/voice/calls.log.jsonl | jq .

# Статус кампании
curl -H "X-Secret: $SECRET" \
  http://localhost:8769/api/v1/campaign/abc123/status | jq .
```

## 📝 Лицензия

Proprietary software. Все права защищены.


