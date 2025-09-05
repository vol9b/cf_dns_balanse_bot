# Используем официальный Python образ
FROM python:3.11-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Создаем рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY bot.py .

# Создаем директорию для данных
RUN mkdir -p /data

# Создаем пользователя для безопасности
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app \
    && chown -R app:app /data
USER app

# Устанавливаем переменные окружения по умолчанию
ENV CF_DB_PATH=/data/cf_dns.db
ENV PING_INTERVAL_SECONDS=10
ENV FLAP_THRESHOLD=3
ENV CF_MANAGE_DNS=true
ENV TELEGRAM_ENABLED=false

# Команда по умолчанию
CMD ["python", "bot.py"]
