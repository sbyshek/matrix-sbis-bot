# 🤖 Matrix VPN Access Bot

Автоматизированный бот для **Matrix**, управляющий временным VPN-доступом через **MikroTik Firewall**. Позволяет сотрудникам получать 15-минутный доступ к ресурсам по запросу через личные сообщения. Бот интегрирован с **Active Directory (LDAP)** для проверки прав и автоматического определения компьютера пользователя.

---

## 🚀 Особенности

- 🔐 **Приватные диалоги 1:1**: Бот работает через личные сообщения Matrix. История видна только пользователю и боту.
- 🛡️ **Контроль доступа через AD**: Проверка членства в группе `ALLOWED_USERS_GROUP` при каждом сообщении.
- 🌐 **Автоматическое определение ПК**: Извлечение `msDS-PrimaryComputer` из LDAP и формирование DNS-имя вида `CN.st-oil.local`.
- 🚪 **Динамическое управление MikroTik**: Добавление/удаление IP в firewall `address-list` с точными таймаутами.
- ⏱️ **Двойной контроль сессий**: Таймауты на стороне MikroTik + внутренний `State Manager` бота.
- 👨‍💼 **Административные функции**: Принудительное отключение, просмотр истории, чтение логов, управление правилами FW, перезапуск служб.
- 🐳 **Docker-first**: Готовая контейнеризация, volumes для логов/данных, live-reload для разработки.
- 📊 **Продвинутое логирование**: Ротируемые файлы, разделение по уровням, корректная обработка часовых поясов.

---

## 🏗️ Архитектура

```
┌─────────┐      ┌──────────┐      ┌──────────      ┌──────────┐
│ Пользов │─────▶│ Matrix   │─────▶│ Бот      │─────▶│ LDAP (AD)│
│         │      │ Сервер   │      │ (Async)  │      └──────────
└─────────┘      └──────────      └──────────┘
                                       │
                                       ▼
                                  ┌──────────┐
                                  │ MikroTik │
                                  │ Firewall │
                                  └──────────
```

**Поток данных:**

1. Пользователь отправляет `!connect` в ЛС боту.
2. Бот проверяет членство в AD-группе.
3. LDAP-запрос: `(&(objectClass=person)(sAMAccountName=user_login))` → получение `msDS-PrimaryComputer`.
4. Формирование IP: `CN.st-oil.local`.
5. MikroTik: `/ip firewall address-list add address=IP list=VPN_ACCESS timeout=900s`.
6. Состояние сохраняется в `data/connection_state.json`.
7. Автоматическое удаление через 15 минут (MikroTik) + очистка в `State Manager`.

---

## 📋 Требования

- Docker & Docker Compose
- Matrix-сервер (Synapse, Conduit и т.д.)
- MikroTik RouterOS с включённым SSH
- Active Directory / LDAP с сервисной учётной записью
- Python 3.11+ (для локальной разработки)

---

## 🛠️ Быстрая установка

1. Клонируйте репозиторий:

   ```bash
   git clone <repository-url>
   cd matrix-vpn-bot
   ```

2. Создайте файл окружения:

   ```bash
   cp .env.example .env
   ```

3. Заполните `.env` (см. раздел **Конфигурация**).

4. Соберите и запустите:

   ```bash
   make build && make up
   ```

5. Проверьте логи:

   ```bash
   make logs
   ```

6. Найдите бота в Matrix-клиенте и начните личный диалог. Бот автоматически примет приглашение и отправит приветственное сообщение.

---

## ⚙️ Конфигурация (`.env`)

| Переменная | Описание | Пример |
|------------|----------|--------|
| **Matrix** | | |
| `MATRIX_HOMESERVER` | Адрес Matrix-сервера (без `https://` и `/` в конце) | `matrix.domain.local` |
| `MATRIX_BOT_USERNAME` | Полный ID бота | `@sbis_bot:domain.local` |
| `MATRIX_BOT_PASSWORD` | Пароль или Access Token бота | `your_password` |
| `MATRIX_DOMAIN` | Домен сервера (для @упоминаний) | `domain.local` |
| **LDAP (AD)** | | |
| `LDAP_SERVER` | Адрес LDAP-сервера | `ldap://dc.domain.local` |
| `LDAP_USER` | Сервисный аккаунт (CN или полный DN) | `CN=bot,OU=SA,DC=domain,DC=local` |
| `LDAP_PASSWORD` | Пароль сервисного аккаунта | `ldap_pass` |
| `LDAP_SEARCHBASE` | База поиска | `DC=domain,DC=local` |
| `ALLOWED_USERS_GROUP` | AD-группа, разрешающая доступ (CN или часть DN) | `VPN_Users` |
| **MikroTik** | | |
| `ROUTEROS_HOST` | IP или хост MikroTik | `192.168.1.1` |
| `ROUTEROS_PORT` | Порт SSH | `22` |
| `ROUTEROS_USERNAME` | Логин для SSH | `admin+ct` |
| `ROUTEROS_PASSWORD` | Пароль для SSH | `mikrotik_pass` |
| `ROUTEROS_ADDRESSLIST` | Имя списка в address-list | `VPN_ACCESS` |
| `ROUTEROS_BLOCK_RULE_COMMENT` | Комментарий для правил FW | `TIME_BLOCK` |
| **Admin & System** | | |
| `ADMIN_LOGINS` | Логины администраторов (через запятую) | `a.avdeev,b.belkin` |
| `TIMEZONE` | Часовой пояс для отображения | `UTC+7` |
| `LOG_LEVEL` | Уровень логирования | `INFO` |
| `LOG_FILE` | Путь к файлу лога внутри контейнера | `/app/logs/bot.log` |
| `LOG_MAX_BYTES` | Макс. размер лога перед ротацией | `10485760` |
| `LOG_BACKUP_COUNT` | Кол-во архивных логов | `5` |

> ⚠️ Никогда не коммитьте `.env` в Git. Файл уже добавлен в `.gitignore`.

---

## 💬 Команды бота

### 👤 Пользовательские команды

| Команда | Описание |
|---------|----------|
| `!connect` | Получить доступ на 15 минут |
| `!connect <минуты>` | Получить доступ на N минут |
| `!disconnect` | Отключиться и освободить IP |
| `!status` | Проверить статус подключения |
| `!help` | Показать справку |

### 🔧 Административные команды

| Команда | Описание |
|---------|----------|
| `!admin_disconnect <user>` | Принудительно отключить пользователя |
| `!check_user <user>` | Проверить AD-атрибуты и членство в группе |
| `!history [N]` | Показать последние N подключений (по умолч. 20) |
| `!log [N]` | Показать последние N строк лога (по умолч. 20) |
| `!pending_list` | Показать список пользователей, ожидающих настройки |
| `!pending_clear` | Очистить список ожидающих |
| `!rule_enable [comment]` | Включить правила FW по комментарию |
| `!rule_disable [comment]` | Отключить правила FW по комментарию |
| `!restart_service` | Перезапустить службу на удалённой Windows-машине |

---

## 📁 Структура проекта

```
matrix-vpn-bot/
├── app/
│   ├── bot.py              # Точка входа и логика Matrix
│   ├── config.py           # Парсинг переменных окружения
│   ├── service/            # Бизнес-логика (LDAP, MikroTik, State)
│   │   ├── ldap.py
│   │   ├── router_os.py
│   │   └── state_manager.py
│   └── models/
│       └── schemas.py      # Pydantic-модели и Enum-ы
├── docker-compose.yml      # Контейнеризация и volumes
├── Dockerfile              # Образ Python 3.11-slim
├── Makefile                # Утилиты сборки и запуска
├── requirements.txt        # Python-зависимости
├── .env                    # Конфигурация (gitignored)
├── .env.example            # Шаблон конфигурации
├── logs/                   # Логи бота (монтируется)
└── data/                   # JSON-состояния (монтируется)
```

---

## 📝 Логирование

- Логи хранятся в `logs/bot.log` на хосте.
- Формат: `2026-04-28 12:00:00 | INFO     | __main__ | Command from user: !connect`
- Просмотр в реальном времени:

  ```bash
  make logs
  # или
  tail -f logs/bot.log
  ```

---

## 🔒 Безопасность

- ✅ Приватные диалоги Matrix (нет общей истории команд)
- ✅ Проверка AD-группы при каждом сообщении
- ✅ Сервисный LDAP-аккаунт с минимальными правами (`memberOf`, `msDS-PrimaryComputer`)
- ✅ Контейнер запускается от `root`, но не экспонирует лишних портов
- ✅ Таймауты предотвращают "висящие" сессии
- ✅ Блокировка одновременного подключения для одного пользователя
- ✅ `.env` и `data/*.json` исключены из Git

---

## 🔍 Устранение проблем

| Проблема | Причина | Решение |
|----------|---------|---------|
| `❌ Не удалось определить ваш компьютер в LDAP` | Атрибут `msDS-PrimaryComputer` пуст или неверен | Заполните атрибут в AD или используйте `!check_user` для диагностики |
| `❌ Ошибка настройки MikroTik` | Неверные SSH-учётные данные или выключен SSH | Проверьте `ssh admin@ROUTEROS_HOST`, включите сервис: `/ip service enable ssh` |
| `Permission denied` при записи в `data/` или `logs/` | Docker создал папки от `root` | Выполните `sudo chown -R $USER:$USER data logs` и `make restart` |
| Доступ остаётся активным после перезапуска | Состояние сохранено в `connection_state.json` | Удалите файл: `rm data/connection_state.json && make restart` |

---

## 🛠️ Разработка

### Live-reload (Dev Mode)

В `docker-compose.yml` настроено монтирование папки `app`:

```yaml
volumes:
  - ./app:/app/app      # Синхронизация кода в реальном времени
  - ./logs:/app/logs    # Логи
  - ./data:/app/data    # Состояние
```

После изменения `bot.py` достаточно выполнить:

```bash
make restart
```
Пересборка образа (`make build`) требуется **только** при изменении `requirements.txt` или `Dockerfile`.

### Локальный запуск без Docker

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app/bot.py
```

---

## 👩‍ Руководство для администраторов

Для подробных инструкций по настройке AD-групп, заполнению атрибутов и работе с административными командами см. файл [`ADMIN_GUIDE.md`](ADMIN_GUIDE.md).

---

## 🤝 Контакт и поддержка

- Для изменений в конфигурации обращайтесь к владельцу сервиса.
- Баг-репорты и предложения: создавайте Issue в репозитории.
- Версия бота: `1.0.0` (Matrix Migration)