.PHONY: help build up down restart logs 




help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'


build: ## Build container
	docker-compose build


up: ## Up container
	docker-compose up -d

down:	## Down container
	docker-compose down

restart: ## Restart container
	docker-compose down && docker-compose up -d && docker-compose logs -f

logs: ## Show logs
	docker-compose logs -f
