# Используем официальный Python образ
FROM python:3.11-slim

# Метаданные образа
LABEL maintainer="cf-dns-bot"
LABEL description="Cloudflare DNS Load Balancer Bot"
LABEL version="1.0"

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Создаем пользователя для безопасности
RUN useradd --create-home --shell /bin/bash --uid 1000 app

# Создаем директории
RUN mkdir -p /app /data /var/log/cf-dns-bot \
    && chown -R app:app /app /data /var/log/cf-dns-bot

# Создаем рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей
COPY --chown=app:app requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY --chown=app:app bot.py .

# Переключаемся на пользователя app
USER app

# Устанавливаем переменные окружения по умолчанию
ENV CF_DB_PATH=/data/cf_dns.db \
    PING_INTERVAL_SECONDS=10 \
    FLAP_THRESHOLD=3 \
    CF_MANAGE_DNS=true \
    TELEGRAM_ENABLED=false \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1


# Команда по умолчанию
CMD ["python", "bot.py"]
