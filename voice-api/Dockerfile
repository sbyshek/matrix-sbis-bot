FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для Whisper (ffmpeg, build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости отдельно для кеширования
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем приложение
COPY app/ ./app/

# Создаём папку для данных
RUN mkdir -p /app/data/voice && chown -R nobody:nogroup /app/data

# Запуск: Gunicorn + Uvicorn workers для продакшена
CMD ["gunicorn", "app.main:app", \
     "--workers", "2", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]