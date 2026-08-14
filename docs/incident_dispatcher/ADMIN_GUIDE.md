# 🛠️ ADMIN GUIDE — настройка и администрирование системы

Руководство администратора для системы управления инцидентами и голосовыми оповещениями (incident-dispatcher + voice-api + Asterisk + Matrix).

---

## 1. Архитектура и потоки данных

```
Браузер (дашборд) ──X-API-Token──▶ incident-dispatcher:8770
Цеховой пульт (IP) ───────────────▶ incident-dispatcher (/desk/{loc_id})
incident-dispatcher ──X-Secret───▶ voice-api:8769 ──AMI──▶ Asterisk
Asterisk ──X-Secret──▶ voice-api (/campaign/finish, /campaign/next)
Asterisk ──X-Secret──▶ voice-api (/campaign/trigger-ivr) ──X-Local-Token──▶ incident-dispatcher (/alert/trigger-by-code)
Asterisk ──X-Secret──▶ incident-dispatcher (/api/v1/audio/*.wav)  # скачивание аудио
incident-dispatcher ──Bearer──▶ Matrix Homeserver
```

**Правило токенов:**
| Кто | Чем авторизуется |
|---|---|
| Дашборд (браузер) | `X-API-Token` = `DASHBOARD_API_TOKEN` (+ IP из `DASHBOARD_ALLOWED_IPS`) |
| Участники Matrix-комнат | `Authorization: Bearer <matrix_token>` (+ участие/права модератора) |
| Цеховой пульт | IP из `allowed_ips` локации |
| voice-api → dispatcher | `X-Local-Token` = `LOCAL_API_TOKEN` |
| Asterisk → voice-api | `X-Secret` = `VOICE_API_SECRET` |
| Asterisk → voice-api (IVR-прокси) → dispatcher | `X-Secret` → `X-Local-Token` |

---

## 2. Первичная установка

### 2.1. Требования

- Docker + Docker Compose
- Asterisk 16+ (chan_sip/IAX2) с доступом по AMI
- Matrix Homeserver с бот-аккаунтом

### 2.2. Запуск

```bash
docker compose --profile all up -d --build
```

Профили: `all`, `dev`, `prod`, `bot`, `voice`, `auto`.

### 2.3. Переменные окружения

**Корневой `.env`:** `REDIS_PASSWORD`, `BOT_TOKEN`, `TIMEZONE=Asia/Novosibirsk`.

**`incident-dispatcher/.env`:**
```env

MATRIX_HOMESERVER=https://matrix.vpk-oil.ru
MATRIX_ACCESS_TOKEN=<токен бота>
DASHBOARD_MATRIX_ROOM_ID=!...:matrix.vpk-oil.ru
DASHBOARD_API_TOKEN=<случайная строка>
DASHBOARD_ALLOWED_IPS=192.168.168.58,10.0.0.0/8
LOCAL_API_TOKEN=<случайная строка>          # для voice-api и Asterisk-прокси
VOICE_API_SECRET=<общий секрет с voice-api>
ASTERISK_API_TOKEN=<для STT-инцидентов>
ALLOWED_IPS=127.0.0.1,172.17.10.10          # для verify_ip (внутренние вызовы)
AUTO_CLOSE_ATTENTION_MINUTES=60
AUTO_CLOSE_ALARM_MINUTES=0
HISTORY_TTL_HOURS=48
```

**`voice-api/.env`:**
```env
VOICE_API_SECRET=<тот же секрет>
REDIS_URL=redis://redis:6379/3
INCIDENT_DISPATCHER_URL=http://incident-dispatcher:8000
INCIDENT_DISPATCHER_TOKEN=<LOCAL_API_TOKEN диспетчера>
WHISPER_MODEL=small
```

---

## 3. Настройка Matrix

1. Создайте комнату на каждую локацию и общую комнату дашборда.
2. Добавьте бота (`MATRIX_ACCESS_TOKEN`) во все комнаты.
3. Для пользователей-операторов: участие в комнате = чтение; **power level ≥ 50** = управление.
4. Токен пользователя: Element → Настройки → Помощь и информация → Access Token.

---

## 4. Конфигурация локаций (`incident-dispatcher/config/locations.json`)

```json
"warehouse": {
  "name": "Склад ГСМ",
  "icon": "🛢️",
  "room_id": "!...:matrix.vpk-oil.ru",
  "asterisk_node": "zv-asterisk",
  "allowed_ips": ["192.168.168.58"],
  "alerts": [
    {
      "template_id": "fire_alarm",
      "trigger_code": "1001",
      "matrix_text": "🚨 **Склад ГСМ: ПОЖАРНАЯ ТРЕВОГА!**\nИнициатор: {author}\nВремя: {time}",
      "subscribers": ["89234743710:ACX:BCX", "4299:IT:Zavod"]
    }
  ]
}
```

- `subscribers` — формат `номер[:код[:описание]]`.
- `allowed_ips` — доступ к цеховому пульту `/desk/{loc_id}` и запись событий без Matrix.
- **Дубли `trigger_code`**: система отрабатывает мягко — 1 совпадение → запуск; N → разбор по номеру звонящего (если он в абонентах совпадения), иначе запуск ВСЕХ + warning в лог. Список дублей: `GET /api/v1/ivr/codes` (поле `duplicates`).

## 5. Шаблоны (`config/templates.json`)

```json
"fire_alarm": {
  "name": "Пожарная тревога",
  "audio_file": "custom/test-message-8000",
  "severity": "alarm",
  "default_text": "🚨 ВНИМАНИЕ! Пожарная тревога..."
}
```

⚠️ `audio_file` хранится **без расширения** `.wav` — диалплан и эндпоинты добавляют его сами.

---

## 6. Аудиофайлы

- Хранилище: `incident-dispatcher/data/audio/custom/` (volume `./incident-dispatcher/data:/app/data`).
- Загрузка: вкладка «🎧 Аудиофайлы» в дашборде или `POST /api/v1/audio/upload` (multipart, `file` + опционально `template_id`). Любой формат конвертируется ffmpeg в **WAV PCM16 mono 8kHz**.
- Отдача Asterisk: `GET /api/v1/audio/{path}.wav` (X-Secret).
- Кэш Asterisk: `/var/lib/asterisk/sounds/cached/` (проверка RIFF-заголовка, `curl --fail`).
- **Защита**: файл, привязанный к шаблону, удалить нельзя (409).
- Миграция старых файлов: `scp root@asterisk:/var/lib/asterisk/sounds/custom/*.wav → data/audio/custom/`.

---

## 7. Asterisk

### 7.1. AMI

`/etc/asterisk/manager.conf`: пользователь `voice_api` с правами `call,command,originate`. Конфиг узлов: `voice-api/config/asterisk_nodes.json` (routing_rules, external_trunks, `max_concurrent_per_trunk`).

### 7.2. Контексты (extensions_custom.conf)

- `pa_call_file_new` — исходящий звонок; после `Dial(...,U(alert_handler^s^1))` — логирование `DIALSTATUS` через `[log-fail]`.
- `alert_handler` — ответ: curl `call_answered` → кэш/скачивание аудио → Playback → curl `call_finished`; `CHANNEL(hangup_handler_push)=alert_hangup` фиксирует «бросил трубку».
- `alert_anchor` — якорь для 1-й половины Local-канала (устраняет «второй звонок»). **В voice-api Originate: `Context: alert_anchor`.**
- `from-internal-custom` → `8000` — IVR: `Read` кода → `ivr_trigger.sh` → voice-api `/campaign/trigger-ivr`.

### 7.3. Скрипт `/usr/local/asterisk/scripts/ivr_trigger.sh`
Обращается **только в voice-api** (порт 8769), возвращает `OK|NOTFOUND|ERR` через `printf` (без `\n`).

### 7.4. Cron-уборка
`/etc/cron.d/asterisk-tmp-cleanup`:
```cron
17 3 * * * root find /tmp -maxdepth 1 -type f -name "voice_*.log" -mtime +1 -delete
```

---

## 8. Эксплуатация

### 8.1. Обновление

- Код (`app/`), конфиги, templates, data — на volumes → **restart** достаточно.
- Изменения `Dockerfile`/`requirements.txt` → `up -d --build`.

### 8.2. Логи и диагностика

```bash
docker logs -f incident-dispatcher
docker logs -f voice-api
tail -f voice-api/data/voice/calls.log.jsonl | jq .
curl -s -H "X-Secret: $SECRET" http://localhost:8769/api/v1/debug/queue
asterisk -rvvv
```

### 8.3. Redis (db 4 — dispatcher, db 3 — voice)
```
dispatch:state:{loc_id} / dispatch:history:{loc_id} / dispatch:unified_history
dispatch:campaign_launch:{id} / voice:active_trunk:{trunk}
```

---

## 9. Troubleshooting (из боевой практики)

| Симптом | Причина / решение |
|---|---|
| 403 от диспетчера при вызове с Asterisk | IP Asterisk не в `ALLOWED_IPS` |
| «Второй пустой звонок» | Originate `Context` должен быть `alert_anchor`, не `pa_call_file_new` |
| Файл не проигрывается | Битый кэш: удалить не-RIFF файлы в `cached/`; curl только с `--fail` |
| Микрофон в студии не работает | Нужен HTTPS (Этап 5) или Chrome flag `unsafely-treat-insecure-origin-as-secure` |
| `Trunk at max capacity` | Зависшие счётчики: `DELETE /api/v1/debug/reset` (voice-api) |
| Статус «answered» без итога | Абонент бросил трубку; после фикса hangup-handler придёт `interrupted` |
| Matrix-сообщение после звонков | Порядок в триггерах: сначала `send_*_to_matrix`, потом voice-api |

---

## 10. Резервное копирование

- `incident-dispatcher/config/`, `data/audio/`
- `voice-api/data/voice/calls.log.jsonl`, `config/`
- volume `redis_data`
- `/etc/asterisk/` + `extensions_custom.conf` + скрипты

## 11. Roadmap

- HTTPS (nginx + wildcard), фикс `verify_ip` под прокси (X-Forwarded-For)
- Статусы `interrupted/noanswer` в UI (бэклог отладки)
- TTS-подсказки IVR, запись через студию