FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости (если понадобятся) и очищаем кэш
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и устанавливаем зависимости (этот слой закэшируется, если requirements не меняются)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код (остальное пробросим через volumes в docker-compose для live-reload)
COPY app/ ./app/
COPY templates/ ./templates/
COPY config/ ./config/

# Запуск приложения
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]