FROM python:3.11-slim

WORKDIR /app

# Системные зависимости не нужны: pysmb чистый Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код копируем ПОСЛЕ установки зависимостей
COPY . .

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-u", "app/bot.py"]