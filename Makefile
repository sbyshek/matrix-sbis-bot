.PHONY: help up down restart build logs status clean prune \
        up-sbis down-sbis restart-sbis logs-sbis build-sbis shell-sbis \
        up-auto down-auto restart-auto logs-auto build-auto shell-auto \
        up-redis down-redis restart-redis logs-redis shell-redis \
        up-f logs-f

COMPOSE := docker compose
COMPOSE_FILE := docker-compose.yml

TASK := task-api
SBIS := sbis-bot
AUTO := auto-bot
REGISTRY := registry-bot
ALERT := alert_bot
CANTEEN := canteen_api
VOICE := voice-api
INCIDENT := incident-dispatcher

# Цвета для вывода
COLOR_RESET := \033[0m
COLOR_GREEN := \033[32m
COLOR_CYAN := \033[36m
COLOR_YELLOW := \033[33m

# ================= ГЛОБАЛЬНЫЕ КОМАНДЫ =================

help: ## 📖 Показать справку по всем командам
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(COLOR_CYAN)%-20s$(COLOR_RESET) %s\n", $$1, $$2}'

build: ## 🏗️ Собрать все образы
	@echo "$(COLOR_YELLOW)🔨 Собираю все сервисы...$(COLOR_RESET)"
	$(COMPOSE) --profile all build

up: ## 🚀 Запустить все сервисы в фоне
	$(COMPOSE) --profile all up -d

down: ## 🛑 Остановить все контейнеры
	$(COMPOSE) --profile all down

restart: ## 🔄 Мягкий рестарт всех контейнеров (без пересборки)
	$(COMPOSE) --profile all restart

up-f: ## 🚀 Запустить всё в foreground с логами
	$(COMPOSE) --profile all up

logs: ## 📜 Логи всех сервисов (follow)
	$(COMPOSE) --profile all logs -f

status: ## 📊 Статус контейнеров
	$(COMPOSE) ps

clean: down ## 🧹 Полная очистка (контейнеры + volumes + образы)
	$(COMPOSE) down -v --rmi local
	docker system prune -f
	@echo "$(COLOR_GREEN)✅ Очистка завершена$(COLOR_RESET)"

prune: ## 🗑️ Удалить dangling образы и кэш
	docker system prune -af

# ================= sbis BOT =================

build-sbis: ## 🏗️ Собрать образ sbis-бота
	$(COMPOSE) build sbis-bot

up-sbis: ## 🚀 Запустить sbis-бота
	$(COMPOSE) up -d sbis-bot

down-sbis: ## 🛑 Остановить sbis-бота
	$(COMPOSE) stop sbis-bot

restart-sbis: ## 🔄 Рестарт sbis-бота
	$(COMPOSE) restart sbis-bot

logs-sbis: ## 📜 Логи sbis-бота
	$(COMPOSE) logs -f sbis-bot

shell-sbis: ## 🐚 Bash внутри контейнера sbis-бота
	$(COMPOSE) exec sbis-bot bash

# ================= AUTO BOT =================

build-auto: ## 🏗️ Собрать образ Auto-бота
	$(COMPOSE) build auto-bot

up-auto: ## 🚀 Запустить Auto-бота
	$(COMPOSE) up -d auto-bot

down-auto: ## 🛑 Остановить Auto-бота
	$(COMPOSE) stop auto-bot

restart-auto: ## 🔄 Рестарт Auto-бота
	$(COMPOSE) restart auto-bot

logs-auto: ## 📜 Логи Auto-бота
	$(COMPOSE) logs -f auto-bot

shell-auto: ## 🐚 Bash внутри контейнера Auto-бота
	$(COMPOSE) exec auto-bot bash

test-glonass: ## 🧪 Запустить диагностику GLONASS внутри контейнера auto-bot
	$(COMPOSE) exec -T auto-bot python /app/app/test_glonass.py

# ================= REDIS / INFRA =================

up-redis: ## 🚀 Запустить Redis
	$(COMPOSE) up -d redis

down-redis: ## 🛑 Остановить Redis
	$(COMPOSE) stop redis

restart-redis: ## 🔄 Рестарт Redis
	$(COMPOSE) restart redis

logs-redis: ## 📜 Логи Redis
	$(COMPOSE) logs -f redis

shell-redis: ## 🐚 Redis CLI
	$(COMPOSE) exec redis redis-cli

# ================= REDIS / INFRA =================

up-reg: ## 🚀 Запустить Registry Bot
	$(COMPOSE) up -d registry-bot

down-reg: ##  Остановить Registry Bot
	$(COMPOSE) stop registry-bot

restart-reg: ##  Рестарт Registry Bot
	$(COMPOSE) restart registry-bot

logs-reg: ##  Логи Registry Bot
	$(COMPOSE) logs -f registry-bot

shell-reg: ## 🐚 Bash в Registry Bot
	$(COMPOSE) exec registry-bot bash

# ================= ALERT API =================

up-alert: ## 🚀 Запустить Alert API + Bot
	$(COMPOSE) up -d alert_api alert_bot

down-alert: ##  Остановить Alert сервисы
	$(COMPOSE) stop alert_api alert_bot

restart-alert: ##  Рестарт Alert сервисов
	$(COMPOSE) restart alert_api alert_bot

logs-alert: ##  Логи Alert сервисов
	$(COMPOSE) logs -f alert_bot 

logs-api: ##  Логи Alert API
	$(COMPOSE) logs -f alert_api

# ================= CANTEEN API =================

up-canteen: ## 🚀 Запустить Alert API + Bot
	$(COMPOSE) up -d canteen_api 

down-canteen: ##  Остановить Alert сервисы
	$(COMPOSE) stop canteen_api

restart-canteen: ##  Рестарт Alert сервисов
	$(COMPOSE) restart canteen_api

logs-canteen: ##  Логи Alert сервисов
	$(COMPOSE) logs -f canteen_api


# ================= TASK API =================

build-task: ## 🚀 Собрать TASK API
	$(COMPOSE) build ${TASK}

up-task: ## 🚀 Запустить TASK API
	$(COMPOSE) up -d ${TASK}

down-task: ##  Остановить TASK сервисы
	$(COMPOSE) stop ${TASK}

restart-task: ##  Рестарт TASK сервисов
	$(COMPOSE) restart ${TASK}

logs-task: ##  Логи TASK сервисов
	$(COMPOSE) logs -f ${TASK}

shell-task: ## 🐚 Bash в Registry Bot
	$(COMPOSE) exec ${TASK} bash


# ================= VOICE API =================

build-voice: ## 🚀 Собрать VOICE API
	$(COMPOSE) build ${VOICE}

up-voice: ## 🚀 Запустить VOICE API
	$(COMPOSE) up -d ${VOICE}

down-voice: ##  Остановить VOICE сервисы
	$(COMPOSE) stop ${VOICE}

restart-voice: ##  Рестарт VOICE сервисов
	$(COMPOSE) restart ${VOICE}

logs-voice: ##  Логи TASK сервисов
	$(COMPOSE) logs -f ${VOICE} --tail 30

shell-voice: ## 🐚 Bash в VOICE
	$(COMPOSE) exec ${VOICE} bash


# ================= INCIDENT DISPATCHER =================

build-incident: ## 🚀 Собрать INCIDENT
	$(COMPOSE) build ${INCIDENT}

up-incident: ## 🚀 Запустить INCIDENT
	$(COMPOSE) up -d ${INCIDENT}

down-incident: ##  Остановить INCIDENT сервисы
	$(COMPOSE) stop ${INCIDENT}

restart-incident: ##  Рестарт VOICE сервисов
	$(COMPOSE) restart ${INCIDENT}

logs-incident: ##  Логи TASK сервисов
	$(COMPOSE) logs -f ${INCIDENT} --tail 30

shell-incident: ## 🐚 Bash в INCIDENT
	$(COMPOSE) exec ${INCIDENT} bash